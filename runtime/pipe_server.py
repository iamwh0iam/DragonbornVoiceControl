import json
import os
import threading
import time
import wave
import sys
from typing import Optional, List
from itertools import cycle
from pathlib import Path
from datetime import datetime

import win32pipe, win32file, pywintypes

from rich.console import Console
from rich.live import Live
from rich.text import Text

from audio_pipeline import AudioPipeline
from recognition import Recognizer
import matching
from config import ServerConfig
from voice_rules import VoiceState
from vosk_models import ensure_vosk_model
from log_utils import setup_timestamped_print, log_warn, log_error, log_success

setup_timestamped_print()



# ===== PIPE =====
PIPE_NAME = r"\\.\pipe\DVC_voice_local"

CMD_OPEN_PREFIX = "OPEN|"
CMD_CLOSE = "CLOSE"
CMD_LISTEN_ON = "LISTEN|1"
CMD_LISTEN_OFF = "LISTEN|0"
CMD_LISTEN_SHOUTS_ON = "LISTEN|SHOUTS|1"
CMD_LISTEN_SHOUTS_OFF = "LISTEN|SHOUTS|0"
CMD_STATE_SHOUT_CONTEXT_PREFIX = "STATE|SHOUT_CONTEXT|"
CMD_LANG_PREFIX = "LANG|"

SR = 16000

_WAIT_CONSOLE = Console(file=sys.__stdout__, force_terminal=True)
_WAIT_BASE = "Waiting for client"
_WAIT_DOTS = cycle(["", ".", "..", "..."])
_WAIT_TS: str | None = None


def _render_waiting() -> Text:
    if _WAIT_TS:
        return Text(f"[{_WAIT_TS}] {_WAIT_BASE}{next(_WAIT_DOTS)}")
    return Text(f"{_WAIT_BASE}{next(_WAIT_DOTS)}")


def _connect_with_wait(pipe) -> None:
    done = threading.Event()
    errors: list[pywintypes.error] = []

    def _connect():
        try:
            win32pipe.ConnectNamedPipe(pipe, None)
        except pywintypes.error as e:
            errors.append(e)
        finally:
            done.set()

    thread = threading.Thread(target=_connect, daemon=True)
    # Capture a single timestamp to print once on the left of the live line
    global _WAIT_TS
    _WAIT_TS = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    thread.start()

    with Live(_render_waiting(), refresh_per_second=10, console=_WAIT_CONSOLE) as live:
        while not done.is_set():
            time.sleep(0.5)
            live.update(_render_waiting())

    thread.join()
    if errors:
        raise errors[0]
    # Clear the timestamp so it won't appear for subsequent waits
    _WAIT_TS = None


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "1" if default else "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)).strip())
    except Exception:
        return default


def _normalize_game_language(raw: str) -> tuple[str, str]:
    value = str(raw or "").strip().lower()
    if not value:
        return "", ""

    norm = "".join(ch for ch in value if ch.isalnum())

    mapping = {
        "en": ("en", "english"),
        "english": ("en", "english"),
        "ru": ("ru", "russian"),
        "russian": ("ru", "russian"),
        "fr": ("fr", "french"),
        "french": ("fr", "french"),
        "it": ("it", "italian"),
        "italian": ("it", "italian"),
        "de": ("de", "german"),
        "german": ("de", "german"),
        "deutsch": ("de", "german"),
        "es": ("es", "spanish"),
        "spanish": ("es", "spanish"),
        "espanol": ("es", "spanish"),
        "pl": ("pl", "polish"),
        "polish": ("pl", "polish"),
        "polski": ("pl", "polish"),
        "ja": ("ja", "japanese"),
        "japanese": ("ja", "japanese"),
        "cn": ("cn", "traditional_chinese"),
        "zh": ("cn", "traditional_chinese"),
        "zhcn": ("cn", "traditional_chinese"),
        "zhhant": ("cn", "traditional_chinese"),
        "chinese": ("cn", "traditional_chinese"),
        "chinesetraditional": ("cn", "traditional_chinese"),
        "traditionalchinese": ("cn", "traditional_chinese"),
    }

    return mapping.get(norm, ("", ""))


def _vosk_model_for_lang(lang_key: str) -> str:
    models = {
        "en": "vosk-model-small-en-us-0.15",
        "fr": "vosk-model-small-fr-0.22",
        "it": "vosk-model-small-it-0.22",
        "de": "vosk-model-small-de-0.15",
        "es": "vosk-model-small-es-0.42",
        "pl": "vosk-model-small-pl-0.22",
        "cn": "vosk-model-small-cn-0.22",
        "ru": "vosk-model-small-ru-0.22",
        "ja": "vosk-model-small-ja-0.22",
    }
    return models.get(lang_key, models["en"])


def _shouts_lang_for_game(lang_key: str) -> str:
    return "ru" if lang_key == "ru" else "en"


def _await_game_language(pipe, reader: "PipeReader") -> tuple[str, str, list[str]]:
    #print("[GAME] waiting for game language from client...", flush=True)
    pending: list[str] = []
    while True:
        line = reader.read_line(pipe)
        if not isinstance(line, str):
            continue
        if line.startswith(CMD_LANG_PREFIX):
            raw = line[len(CMD_LANG_PREFIX):].strip()
            lang_key, lang_label = _normalize_game_language(raw)
            if not lang_key:
                log_warn(f"[GAME][WARN] unknown game language '{raw}', defaulting to english")
                lang_key, lang_label = "en", "english"
            print(f"[GAME] game language detected: {lang_label}", flush=True)
            return lang_key, lang_label, pending
        pending.append(line)


def _apply_language_to_cfg(cfg: ServerConfig, lang_key: str) -> None:
    vosk_override = _env_str("DVC_VOSK_MODEL", str(getattr(cfg, "vosk_model", ""))).strip()
    shouts_model_override = _env_str(
        "DVC_SHOUTS_VOSK_MODEL", str(getattr(cfg, "shouts_vosk_model", ""))
    ).strip()
    shouts_lang_override = _env_str(
        "DVC_SHOUTS_LANG", str(getattr(cfg, "shouts_language", ""))
    ).strip()
    asr_lang_override = _env_str("DVC_ASR_LANG", "").strip()

    cfg.vosk_model = vosk_override or _vosk_model_for_lang(lang_key)
    cfg.shouts_vosk_model = shouts_model_override or _vosk_model_for_lang("ru" if lang_key == "ru" else "en")
    cfg.shouts_language = shouts_lang_override or _shouts_lang_for_game(lang_key)
    cfg.asr_lang = asr_lang_override or (cfg.asr_lang if cfg.asr_lang_specified else "") or lang_key


def _ensure_main_vosk_model(cfg: ServerConfig) -> str:
    cache_root_env = os.environ.get("DVC_CACHE_DIR", "").strip()
    cache_root = Path(cache_root_env).expanduser().resolve() if cache_root_env else (
        Path(sys.executable).resolve().parent if bool(getattr(sys, "frozen", False)) else Path(__file__).resolve().parent
    )
    cache_dir = (cache_root / "caches" / "vosk").resolve()
    model_dir = ensure_vosk_model(cfg.vosk_model, cache_dir)
    os.environ["DVC_VOSK_MODEL_PATH"] = str(model_dir)
    return str(model_dir)


def _save_debug_wav(kind: str, pcm16, rec: Recognizer) -> str | None:
    try:
        if not bool(getattr(rec, "save_wav_enabled", False)):
            return None
        if pcm16 is None:
            return None
        if getattr(pcm16, "size", 0) == 0:
            return None

        cache_root_env = os.environ.get("DVC_CACHE_DIR", "").strip()
        cache_root = Path(cache_root_env).expanduser().resolve() if cache_root_env else (
            Path(sys.executable).resolve().parent if bool(getattr(sys, "frozen", False)) else Path(__file__).resolve().parent
        )
        rel_dir = str(getattr(rec, "wav_dir_rel", "caches/vad_caps") or "caches/vad_caps")
        out_dir = (cache_root / rel_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        ms = int(time.time() * 1000) % 1000
        safe_kind = (kind or "audio").strip().lower().replace(" ", "_")
        filename = f"{safe_kind}_{ts}_{ms:03d}.wav"
        out_path = out_dir / filename

        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(pcm16.tobytes())

        rel_dir_norm = rel_dir.strip("/\\")
        rel_path = f"{rel_dir_norm}/{filename}".replace("\\", "/")
        print(f"[WAV] save_wav=on record_saved={rel_path}", flush=True)
        return rel_path
    except Exception as e:
        log_warn(f"[WAV][WARN] failed to save {kind} wav: {e}")
        return None


def _normalize_shout_id(raw: str | None) -> str:
    if raw is None:
        return ""
    out = str(raw).strip().upper()
    out = out.replace(" ", "_")
    out = out.replace("-", "_")
    return out


def _load_shouts_map_names() -> dict[str, str]:
    try:
        runtime_dir = Path(sys.executable).resolve().parent if bool(getattr(sys, "frozen", False)) else Path(__file__).resolve().parent
        path = runtime_dir / "shouts_map.json"
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out: dict[str, str] = {}
        for sid, entry in data.items():
            key = _normalize_shout_id(sid)
            name = str((entry or {}).get("name", "")).strip()
            if key and name:
                out[key] = name
        return out
    except Exception:
        return {}

# ---------------- PIPE UTILS ----------------
class PipeReader:
    """Buffered line reader for named pipe with buffer visibility."""

    def __init__(self):
        self._buf = b""

    def read_line(self, h):
        while b"\n" not in self._buf:
            _, chunk = win32file.ReadFile(h, 4096)
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8", errors="replace")

    def has_buffered_line(self) -> bool:
        """True if the internal buffer already contains a complete line."""
        return b"\n" in self._buf


def make_reader():
    """Legacy wrapper - returns (read_line_func, PipeReader instance)."""
    reader = PipeReader()
    return reader.read_line, reader

def write_line(h, s):
    win32file.WriteFile(h, (s + "\n").encode("utf-8"))


def write_dbg_line(h, text: str):
    msg = str(text).replace("\r", " ").replace("\n", " ").strip()
    if not msg:
        return
    write_line(h, f"DBG|{msg}")


def _peek_item_has_data(item) -> bool:
    if isinstance(item, int):
        return item > 0
    if isinstance(item, (bytes, bytearray)):
        return len(item) > 0
    return False


def _peek_tuple_common_shape_has_data(info: tuple) -> bool:
    if len(info) < 2:
        return False
    data, avail = info[0], info[1]
    if not isinstance(data, (bytes, bytearray)):
        return False
    avail_int = avail if isinstance(avail, int) else 0
    return (avail_int > 0) or (len(data) > 0)


def _peek_result_has_data(info) -> bool:
    if not isinstance(info, tuple):
        return False
    if _peek_tuple_common_shape_has_data(info):
        return True
    return any(_peek_item_has_data(item) for item in info)

def pipe_has_data(h) -> bool:
    try:
        return _peek_result_has_data(win32pipe.PeekNamedPipe(h, 1))
    except Exception:
        return True

def read_open_packet(read_line, pipe):
    options = []
    while True:
        l2 = read_line(pipe)
        if l2 == "END":
            break
        if l2.startswith("OPT|"):
            options.append(l2[4:])
    return options


def _dialog_grammar(options: list[str]) -> tuple[list[str], str | None]:
    phrases = matching.build_dialog_grammar_phrases(options)
    if not phrases:
        return [], None
    return phrases, json.dumps(phrases, ensure_ascii=False)


def _close_grammar() -> tuple[list[str], str | None]:
    phrases = matching.get_close_phrases_list()
    if not phrases:
        return [], None
    return phrases, json.dumps(phrases, ensure_ascii=False)


def _merge_grammar(*phrase_groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in phrase_groups:
        for phrase in group:
            if phrase and phrase not in seen:
                seen.add(phrase)
                merged.append(phrase)
    return merged


def _try_read_cfg_packet(line: str, *, state: dict, rec: Recognizer) -> tuple[str, bool] | None:
    if not isinstance(line, str) or not line.startswith("CFG|"):
        return None

    parts = line.split("|")
    if len(parts) != 3:
        return None

    kind = parts[1].strip().upper()
    enabled = parts[2].strip().lower() in ("1", "true", "yes", "on")

    cfg_targets = {
        "OPEN": ("open_enable_open", "DVC_OPEN_ENABLE_OPEN"),
        "CLOSE": ("close_enable_voice", "DVC_CLOSE_ENABLE_VOICE"),
        "DIALOGUE_SELECT": ("dialogue_select_enable", "DVC_DIALOGUE_SELECT_ENABLE"),
        "SHOUTS": ("shouts_enable", "DVC_SHOUTS_ENABLE"),
        "POWERS": ("powers_enable", "DVC_POWERS_ENABLE"),
        "DEBUG": ("debug_enabled", "DVC_DEBUG"),
        "SAVE_WAV": ("save_wav_enabled", "DVC_SAVE_WAV"),
        "WEAPONS": ("weapons_enable", "DVC_WEAPONS_ENABLE"),
        "SPELLS": ("spells_enable", "DVC_SPELLS_ENABLE"),
        "POTIONS": ("potions_enable", "DVC_POTIONS_ENABLE"),
    }

    target = cfg_targets.get(kind)
    if target is None:
        return None

    state_key, env_key = target
    state[state_key] = bool(enabled)
    os.environ[env_key] = "1" if enabled else "0"

    if kind == "DEBUG":
        try:
            rec.set_debug_enabled(enabled)
        except Exception as e:
            log_warn(f"[CFG][WARN] failed to apply DEBUG to recognizer: {e}")

    if kind == "SAVE_WAV":
        try:
            rec.set_save_wav_enabled(enabled)
        except Exception as e:
            log_warn(f"[CFG][WARN] failed to apply SAVE_WAV to recognizer: {e}")

    if kind == "SHOUTS" and enabled:
        try:
            if not rec.warmup_shouts():
                log_warn("[SHOUT][WARN] warmup skipped/failed after CFG|SHOUTS|1")
        except Exception as e:
            log_warn(f"[SHOUT][WARN] warmup failed after CFG|SHOUTS|1: {e}")

    return (kind, enabled)


def _try_read_state_packet(line: str) -> tuple[str, bool] | None:
    if not isinstance(line, str) or not line.startswith("STATE|"):
        return None

    parts = line.split("|")
    if len(parts) != 3:
        return None

    kind = parts[1].strip().upper()
    enabled = parts[2].strip().lower() in ("1", "true", "yes", "on")
    if kind not in ("SHOUT_CONTEXT", "DRAWN", "COMBAT"):
        return None

    return (kind, enabled)


def _print_dialog_options(
    options: list[str],
    dialog_grammar_json: str | None,
    close_grammar_json: str | None,
    audio: AudioPipeline,
) -> None:
    print("\n[DIALOG OPENED] OPTIONS:")
    for i, o in enumerate(options, 1):
        print(f" {i}. {o}")
    if dialog_grammar_json:
        print(f"[DIALOG GRAMMAR] {dialog_grammar_json}", flush=True)
    if close_grammar_json:
        print(f"[CLOSE GRAMMAR] {close_grammar_json}", flush=True)
    if audio.mode == "ptt":
        print(f"\nPTT: hold {audio.hotkey.upper()} and speak…", flush=True)
    else:
        print("\nVAD: Listening... (speak, the recording will start automatically)", flush=True)


def _print_dialog_update(
    options: list[str],
    dialog_grammar_json: str | None,
    close_grammar_json: str | None,
) -> None:
    print("\n[DIALOG UPDATED] OPTIONS:")
    for i, o in enumerate(options, 1):
        print(f" {i}. {o}")
    if dialog_grammar_json:
        print(f"[DIALOG GRAMMAR] {dialog_grammar_json}", flush=True)
    if close_grammar_json:
        print(f"[CLOSE GRAMMAR] {close_grammar_json}", flush=True)


def _print_dialog_closed() -> None:
    print("\n[DIALOG CLOSED]\n", flush=True)
class ClientSession:
    def __init__(
        self,
        *,
        pipe,
        audio: AudioPipeline,
        rec: Recognizer,
        open_enable_open: bool,
        shouts_enable: bool,
        close_enable_voice: bool,
        prefetch_lines: Optional[List[str]] = None,
        reader: Optional["PipeReader"] = None,
    ):
        self.pipe = pipe
        self.audio = audio
        self.rec = rec
        if reader is None:
            self.read_line, self._reader = make_reader()
        else:
            self._reader = reader
            self.read_line = reader.read_line
        self._prefetch_lines = list(prefetch_lines or [])

        self.listen_mode = False
        self.dialog_mode = False
        self.listen_commands = False
        self.listen_commands_before_dialog = False
        self.shout_context_allowed = False
        self.player_drawn = False
        self.player_combat = False
        self.options: list[str] = []
        self.dialog_grammar_phrases: list[str] = []
        self.dialog_grammar_json: str | None = None
        self.close_grammar_phrases: list[str] = []
        self.close_grammar_json: str | None = None
        self.await_dialog_open_until = 0.0

        self.last_listen_state = None
        self.last_command_no_match_ts = 0.0
        self._pending_cfg_log: list[str] = []
        self._pending_cfg_reason: str | None = None
        self._pending_cfg_effective = False
        self.voice_state = VoiceState()
        self._shout_id_to_name: dict[str, str] = {}
        self._shouts_map_name: dict[str, str] = _load_shouts_map_names()

        self.state = {
            "open_enable_open": bool(open_enable_open),
            "close_enable_voice": bool(close_enable_voice),
            "shouts_enable": bool(shouts_enable),
            "powers_enable": False,
            "dialogue_select_enable": True,
            "weapons_enable": False,
            "spells_enable": False,
            "potions_enable": False,
            "debug_enabled": bool(getattr(rec, "debug_enabled", False)),
            "save_wav_enabled": bool(getattr(rec, "save_wav_enabled", False)),
        }

        self.audio.set_abort_checker(self._has_pending_data)

    def _feature_enabled_snapshot(self) -> dict[str, bool]:
        return {
            "select": bool(self.state.get("dialogue_select_enable", False)),
            "open": bool(self.state.get("open_enable_open", False)),
            "close": bool(self.state.get("close_enable_voice", False)),
            "shouts": bool(self.state.get("shouts_enable", False)),
            "powers": bool(self.state.get("powers_enable", False)),
            "weapons": bool(self.state.get("weapons_enable", False)),
            "spells": bool(self.state.get("spells_enable", False)),
            "potions": bool(self.state.get("potions_enable", False)),
        }

    def _open_listen_active(self) -> bool:
        return bool(self.listen_mode and self.state.get("open_enable_open", False) and (not self.player_combat))

    def _open_priority_active(self) -> bool:
        return self._open_listen_active()

    def _commands_listen_active(self) -> bool:
        return bool(self.listen_commands and (not self._open_priority_active()))

    def _effective_state(self) -> dict[str, bool]:
        self.voice_state.set_feature_enabled(self._feature_enabled_snapshot())
        self.voice_state.set_dialog_open(self.dialog_mode)
        self.voice_state.set_open_listen(self._open_listen_active())
        self.voice_state.set_commands_listen(self._commands_listen_active())
        self.voice_state.set_shout_context_allowed(self.shout_context_allowed)
        return self.voice_state.effective()

    def _send_effective_if_changed(self, *, reason: str | None = None) -> None:
        self.voice_state.set_feature_enabled(self._feature_enabled_snapshot())
        self.voice_state.set_dialog_open(self.dialog_mode)
        self.voice_state.set_open_listen(self._open_listen_active())
        self.voice_state.set_commands_listen(self._commands_listen_active())
        self.voice_state.set_shout_context_allowed(self.shout_context_allowed)
        changed, effective = self.voice_state.effective_changed()
        if not changed:
            return
        effective_line = self.voice_state.format_effective(effective)
        write_line(self.pipe, effective_line)
        if reason:
            print(
                f"[LISTEN][STATE] {reason} (focus={'ON' if self.listen_mode else 'OFF'} drawn={'ON' if self.player_drawn else 'OFF'} combat={'ON' if self.player_combat else 'OFF'} dialog={'ON' if self.dialog_mode else 'OFF'}) | {effective_line}",
                flush=True,
            )
        print(self.voice_state.format_listen_status(effective), flush=True)

    def _has_pending_data(self) -> bool:
        """Check both the reader's internal buffer and the OS pipe for data."""
        return bool(self._prefetch_lines) or self._reader.has_buffered_line() or pipe_has_data(self.pipe)

    def _next_line(self) -> str:
        if self._prefetch_lines:
            return self._prefetch_lines.pop(0)
        return self._rl()

    def _send_debug_notification(self, text: str) -> None:
        if not bool(self.state.get("debug_enabled", False)):
            return
        write_dbg_line(self.pipe, text)

    def _format_trigger_name(self, raw_text: str | None) -> str:
        name = (raw_text or "").strip()
        return name if name else "?"

    def _rl(self):
        return self.read_line(self.pipe)

    def _queue_cfg_log(self, reason: str, kind: str, enabled: bool) -> None:
        if self._pending_cfg_reason and self._pending_cfg_reason != reason:
            self._flush_cfg_log(force=True)
        if not self._pending_cfg_reason:
            self._pending_cfg_reason = reason
        self._pending_cfg_log.append(f"{kind}={1 if enabled else 0}")

    def _flush_cfg_log(self, *, force: bool = False) -> None:
        if not self._pending_cfg_log:
            return
        reason = self._pending_cfg_reason or "cfg update"
        cfg_str = " ".join(self._pending_cfg_log)
        self._pending_cfg_log = []
        self._pending_cfg_reason = None
        # If reason contains a parenthetical like "(idle)" or "(dialog)",
        # strip it so the left side becomes e.g. "cfg update POTIONS=1" and
        # the right side prints the full current state — avoids duplicating
        # the option list when flushing from idle/dialog contexts.
        base_reason = reason
        if "(" in base_reason:
            base_reason = base_reason.split("(", 1)[0].strip()
        self._log_listen_state(base_reason, force=True)
        if self._pending_cfg_effective:
            self._pending_cfg_effective = False
            self._send_effective_if_changed()

    def _log_listen_state(self, reason: str, *, force: bool = False) -> None:
        effective = self._effective_state()
        snap = (
            bool(self.listen_mode),
            bool(self.listen_commands),
            bool(self.shout_context_allowed),
            bool(self.player_drawn),
            bool(self.player_combat),
            bool(self.dialog_mode),
            tuple(int(effective.get(k, False)) for k in effective.keys()),
        )
        if (not force) and (snap == self.last_listen_state):
            return
        self.last_listen_state = snap
        # For configuration updates, keep the log compact: the caller includes the
        # changed cfg tokens (e.g. "POTIONS=0") in `reason`, so avoid printing
        # the focus parenthetical and the full "effective:" summary there.
        if isinstance(reason, str) and reason.strip().startswith("cfg update"):
            # Print a compact cfg update line with current option states.
            keys_map = [
                ("OPEN", "open_enable_open"),
                ("CLOSE", "close_enable_voice"),
                ("SHOUTS", "shouts_enable"),
                ("POWERS", "powers_enable"),
                ("DEBUG", "debug_enabled"),
                ("SAVE_WAV", "save_wav_enabled"),
                ("DIALOGUE_SELECT", "dialogue_select_enable"),
                ("WEAPONS", "weapons_enable"),
                ("SPELLS", "spells_enable"),
                ("POTIONS", "potions_enable"),
            ]
            parts = [f"{label}={1 if self.state.get(key, False) else 0}" for (label, key) in keys_map]
            cfg_line = " ".join(parts)
            print(f"[LISTEN][STATE] {reason} | {cfg_line}", flush=True)
            return

        effective_line = self.voice_state.format_effective(effective)
        print(
            f"[LISTEN][STATE] {reason} (focus={'ON' if self.listen_mode else 'OFF'} drawn={'ON' if self.player_drawn else 'OFF'} combat={'ON' if self.player_combat else 'OFF'} dialog={'ON' if self.dialog_mode else 'OFF'}) | {effective_line}",
            flush=True,
        )

    def _reset_dialog_state(self) -> None:
        self.dialog_mode = False
        self.dialog_grammar_phrases = []
        self.dialog_grammar_json = None
        self.close_grammar_phrases = []
        self.close_grammar_json = None
        self.rec.clear_dialog_grammar()

    def _open_dialog(self, new_options: list[str], *, reason: str) -> None:
        self.options = new_options
        self.dialog_grammar_phrases, self.dialog_grammar_json = _dialog_grammar(self.options)
        self.close_grammar_phrases, self.close_grammar_json = _close_grammar()
        merged_grammar = _merge_grammar(self.dialog_grammar_phrases, self.close_grammar_phrases)
        self.rec.set_dialog_grammar(merged_grammar)
        self.dialog_mode = True
        self.listen_mode = False
        self.listen_commands_before_dialog = self.listen_commands
        self.listen_commands = False
        effective_line = self.voice_state.format_effective(self._effective_state())
        print("[info] [DIALOG] OPEN", flush=True)
        print(f"[info] [DVC_SERVER] Dialogue opened | {effective_line}", flush=True)
        self._send_effective_if_changed(reason=reason)
        _print_dialog_options(self.options, self.dialog_grammar_json, self.close_grammar_json, self.audio)

    def _update_dialog(self, new_options: list[str]) -> None:
        self.options = new_options
        self.dialog_grammar_phrases, self.dialog_grammar_json = _dialog_grammar(self.options)
        self.close_grammar_phrases, self.close_grammar_json = _close_grammar()
        merged_grammar = _merge_grammar(self.dialog_grammar_phrases, self.close_grammar_phrases)
        self.rec.set_dialog_grammar(merged_grammar)
        _print_dialog_update(self.options, self.dialog_grammar_json, self.close_grammar_json)

    def _close_dialog(self, *, reason: str) -> None:
        _print_dialog_closed()
        self._reset_dialog_state()
        self.listen_commands = self.listen_commands_before_dialog
        effective_line = self.voice_state.format_effective(self._effective_state())
        print("[info] [DIALOG] CLOSE", flush=True)
        print(f"[info] [DVC_SERVER] Dialogue closed | {effective_line}", flush=True)
        self._send_effective_if_changed(reason=reason)

    def _handle_cfg_or_state(self, line: str, *, reason: str) -> bool:
        cfg = _try_read_cfg_packet(line, state=self.state, rec=self.rec)
        if cfg is not None:
            kind, enabled = cfg
            self._queue_cfg_log(reason, kind, enabled)
            self._pending_cfg_effective = True
            if not self._has_pending_data():
                self._flush_cfg_log(force=True)
            return True

        state_update = _try_read_state_packet(line)
        if state_update is not None:
            kind, enabled = state_update
            if kind == "SHOUT_CONTEXT":
                self.shout_context_allowed = bool(enabled)
            elif kind == "DRAWN":
                self.player_drawn = bool(enabled)
            elif kind == "COMBAT":
                self.player_combat = bool(enabled)
            self._send_effective_if_changed(reason=f"state update {kind}={1 if enabled else 0}")
            return True

        return False

    def _parse_favorite_packet_line(
        self,
        line: str,
        shouts: list[tuple[str, str, str, str]],
        powers: list[tuple[str, str]],
        weapons: list[tuple[str, str]],
        spells: list[tuple[str, str]],
        potions: list[tuple[str, str]],
    ) -> None:
        if not isinstance(line, str) or not line.startswith("FAV|"):
            return

        parts = line.split("|")
        if len(parts) < 2:
            return

        kind = parts[1].strip().upper()
        if kind == "SHOUT" and len(parts) >= 6:
            shouts.append((parts[2], parts[3], parts[4], parts[5]))
            return
        if len(parts) < 4:
            return
        if kind == "POWER":
            powers.append((parts[2], parts[3]))
        elif kind == "WEAPON":
            weapons.append((parts[2], parts[3]))
        elif kind == "SPELL":
            spells.append((parts[2], parts[3]))
        elif kind == "POTION":
            potions.append((parts[2], parts[3]))

    def _collect_favorites_payload(self) -> tuple[
        list[tuple[str, str, str, str]],
        list[tuple[str, str]],
        list[tuple[str, str]],
        list[tuple[str, str]],
        list[tuple[str, str]],
    ]:
        shouts: list[tuple[str, str, str, str]] = []
        powers: list[tuple[str, str]] = []
        weapons: list[tuple[str, str]] = []
        spells: list[tuple[str, str]] = []
        potions: list[tuple[str, str]] = []

        while True:
            line = self._rl()
            if line == "FAV|END":
                return shouts, powers, weapons, spells, potions
            self._parse_favorite_packet_line(line, shouts, powers, weapons, spells, potions)

    def _favorite_names(self, entries: list[tuple], name_index: int) -> list[str]:
        names: list[str] = []
        for entry in entries:
            if len(entry) <= name_index:
                continue
            name = str(entry[name_index] or "").strip()
            if name:
                names.append(name)
        return names

    def _format_favorite_names(self, names: list[str]) -> str:
        return ", ".join([f'"{name}"' for name in names])

    def _update_shout_name_map(self, shouts: list[tuple[str, str, str, str]]) -> None:
        self._shout_id_to_name = {}
        for entry in shouts:
            if len(entry) < 4:
                continue
            name = str(entry[2] or "").strip()
            editor_id = _normalize_shout_id(entry[3])
            if editor_id and name:
                self._shout_id_to_name[editor_id] = name

    def _log_favorites_state(
        self,
        shouts: list[tuple[str, str, str, str]],
        powers: list[tuple[str, str]],
        weapons: list[tuple[str, str]],
        spells: list[tuple[str, str]],
        potions: list[tuple[str, str]],
    ) -> None:
        print(
            "[FAVORITES][STATE] Fav updated: "
            f"shouts [{self._format_favorite_names(self._favorite_names(shouts, 2))}], "
            f"powers [{self._format_favorite_names(self._favorite_names(powers, 1))}], "
            f"weapons [{self._format_favorite_names(self._favorite_names(weapons, 1))}], "
            f"spells [{self._format_favorite_names(self._favorite_names(spells, 1))}], "
            f"potions [{self._format_favorite_names(self._favorite_names(potions, 1))}]",
            flush=True,
        )

    def _voice_command_features_available(self) -> bool:
        return bool(
            self.state.get("shouts_enable", False)
            or self.state.get("powers_enable", False)
            or self._any_items_enabled()
        )

    def _set_listen_commands_state(self, enabled: bool, *, reason: str) -> None:
        if enabled:
            if self._voice_command_features_available():
                self.listen_commands = True
            else:
                print("[LISTEN|SHOUTS] ignored: no voice command features enabled", flush=True)
        else:
            self.listen_commands = False
        self._send_effective_if_changed(reason=reason)

    def _set_idle_listen_mode(self) -> None:
        self.listen_mode = bool(self.state["open_enable_open"] or self._any_command_enabled())
        self._send_effective_if_changed(reason="LISTEN ON (idle)")

    def _handle_favorites_packet(self, line: str) -> bool:
        if line != "FAV|BEGIN":
            return False

        shouts, powers, weapons, spells, potions = self._collect_favorites_payload()

        try:
            self.rec.set_allowed_shout_entries(shouts)
            self.rec.set_allowed_power_entries(powers)
            self.rec.set_allowed_weapons_entries(weapons)
            self.rec.set_allowed_spells_entries(spells)
            self.rec.set_allowed_potions_entries(potions)
        except Exception as e:
            log_warn(f"[FAVORITES][WARN] failed to apply favorites: {e}")
            return True

        self._update_shout_name_map(shouts)
        self._log_favorites_state(shouts, powers, weapons, spells, potions)

        if self.state.get("debug_enabled", False):
            self._send_debug_notification("Fav updated")

        return True

    def _handle_non_dialog_line(self, line: str) -> bool:
        if self._handle_favorites_packet(line):
            return True
        if self._handle_cfg_or_state(line, reason="cfg update"):
            return True
        if self._pending_cfg_log:
            self._flush_cfg_log(force=True)
        if line == CMD_CLOSE:
            self._reset_dialog_state()
            self.listen_commands = self.listen_commands_before_dialog
            self._send_effective_if_changed(reason="dialog CLOSE")
            return True
        if line == CMD_LISTEN_OFF:
            self.listen_mode = False
            self._send_effective_if_changed(reason="LISTEN OFF")
            return True
        if line == CMD_LISTEN_ON:
            self.listen_mode = True
            self._send_effective_if_changed(reason="LISTEN ON")
            return True
        if line == CMD_LISTEN_SHOUTS_ON:
            self._set_listen_commands_state(True, reason="LISTEN COMMANDS ON")
            return True
        if line == CMD_LISTEN_SHOUTS_OFF:
            self._set_listen_commands_state(False, reason="LISTEN COMMANDS OFF")
            return True
        if line.startswith(CMD_OPEN_PREFIX):
            self._open_dialog(read_open_packet(self.read_line, self.pipe), reason="dialog OPEN")
            return False
        return True

    def _handle_dialog_line(self, line: str) -> bool:
        if self._handle_favorites_packet(line):
            return True
        if self._handle_cfg_or_state(line, reason="cfg update (dialog)"):
            return True
        if self._pending_cfg_log:
            self._flush_cfg_log(force=True)
        if line.startswith(CMD_OPEN_PREFIX):
            self._update_dialog(read_open_packet(self.read_line, self.pipe))
            return True
        if line == CMD_CLOSE:
            self._close_dialog(reason="dialog CLOSE")
            return True
        if line == CMD_LISTEN_SHOUTS_ON:
            self.listen_commands_before_dialog = True
            return True
        if line == CMD_LISTEN_SHOUTS_OFF:
            self.listen_commands_before_dialog = False
            return True
        if line == CMD_LISTEN_ON:
            self._reset_dialog_state()
            self.listen_mode = True
            self.listen_commands = self.listen_commands_before_dialog
            self._send_effective_if_changed(reason="dialog CLOSE")
            return True
        if line == CMD_LISTEN_OFF:
            self.listen_mode = False
            self._send_effective_if_changed(reason="LISTEN OFF")
            return True
        return False

    def _handle_idle_line(self, line: str) -> None:
        if self._handle_favorites_packet(line):
            return
        if self._handle_cfg_or_state(line, reason="cfg update (idle)"):
            return
        if self._pending_cfg_log:
            self._flush_cfg_log(force=True)
        if line.startswith(CMD_OPEN_PREFIX):
            self._open_dialog(read_open_packet(self.read_line, self.pipe), reason="dialog OPEN (idle)")
            return
        if line == CMD_LISTEN_ON:
            self._set_idle_listen_mode()
            return
        if line == CMD_LISTEN_SHOUTS_ON:
            self._set_listen_commands_state(True, reason="LISTEN COMMANDS ON (idle)")
            return
        if line == CMD_LISTEN_SHOUTS_OFF:
            self._set_listen_commands_state(False, reason="LISTEN COMMANDS OFF (idle)")
            return
        if line == CMD_LISTEN_OFF:
            self.listen_mode = False
            self._send_effective_if_changed(reason="LISTEN OFF (idle)")

    def _format_phrase_set(self, items: list[str]) -> str:
        return "{" + ", ".join([f'"{value}"' for value in items]) + "}"

    def _format_shout_grammar_block(
        self,
        header: str,
        shout_detail: dict[str, list[str]] | None,
    ) -> str:
        parts = []
        for sid in sorted((shout_detail or {}).keys()):
            sid_key = _normalize_shout_id(sid)
            variants = list((shout_detail or {}).get(sid) or [])
            if not variants:
                continue
            shout_name = (
                self._shout_id_to_name.get(sid_key)
                or self._shouts_map_name.get(sid_key, "")
            ).strip()
            if shout_name:
                parts.append(f"{shout_name} {sid_key} {self._format_phrase_set(variants)}")
            else:
                parts.append(f"{sid_key} {self._format_phrase_set(variants)}")
        if parts:
            return f"Shouts: {header} [{', '.join(parts)}]"
        return f"Shouts: {header}"

    def _format_command_grammar_block(
        self,
        tag: str,
        entries: int,
        phrases: int,
        phrase_list: list[str],
        lang: str | None = None,
        *,
        shout_detail: dict[str, list[str]] | None = None,
    ) -> str:
        lang_part = f" grammar_lang={lang}" if lang else ""
        header = f"grammar_entries={entries} phrases={phrases}{lang_part}"
        if tag == "SHOUT":
            return self._format_shout_grammar_block(header, shout_detail)
        label = tag.capitalize() + "s"
        if phrase_list:
            quoted = [f'"{phrase}"' for phrase in phrase_list]
            return f"{label}: {header} [{', '.join(quoted)}]"
        return f"{label}: {header}"

    def _attempted_command_grammar_parts(self, attempted: list[str]) -> list[str]:
        def _append_if_available(tag: str, entries: int, phrases: int, phrase_list: list[str], lang: str | None = None, *, shout_detail: dict[str, list[str]] | None = None) -> None:
            if entries <= 0 and phrases <= 0 and not phrase_list and not shout_detail:
                return
            grammar_parts.append(
                self._format_command_grammar_block(tag, entries, phrases, phrase_list, lang, shout_detail=shout_detail)
            )

        attempted_set = set(attempted)
        grammar_parts: list[str] = []

        if "shout" in attempted_set:
            sh_entries, sh_phrases, sh_list, sh_lang, sh_detail = self.rec.get_shout_grammar_info()
            _append_if_available("SHOUT", sh_entries, sh_phrases, sh_list, sh_lang, shout_detail=sh_detail)

        if "spell" in attempted_set:
            sp_entries, sp_phrases, sp_list = self.rec.get_spell_grammar_info()
            _append_if_available("SPELL", sp_entries, sp_phrases, sp_list)

        if "power" in attempted_set:
            pw_entries, pw_phrases, pw_list = self.rec.get_power_grammar_info()
            _append_if_available("POWER", pw_entries, pw_phrases, pw_list)

        if "weapon" in attempted_set:
            we_entries, we_phrases, we_list = self.rec.get_weapon_grammar_info()
            _append_if_available("WEAPON", we_entries, we_phrases, we_list)

        if "potion" in attempted_set:
            po_entries, po_phrases, po_list = self.rec.get_potion_grammar_info()
            _append_if_available("POTION", po_entries, po_phrases, po_list)

        return grammar_parts

    def _build_command_no_match_detail_parts(
        self,
        vad_stats: dict | None,
        attempted: list[str],
        shout_dbg: dict | None,
        command_dbg: dict | None,
    ) -> tuple[list[str], str]:
        utt = None
        tsil = None
        if isinstance(vad_stats, dict):
            utt = vad_stats.get("utt_sec")
            tsil = vad_stats.get("tail_sil_ms")

        backend = str(self.rec.asr_engine or "?")
        reason = str((command_dbg or {}).get("reason") or (shout_dbg or {}).get("reason") or "no_match")
        recognized_text = str((command_dbg or {}).get("raw_text") or (command_dbg or {}).get("text") or "").strip()
        tp_reason = (shout_dbg or {}).get("two_phase_reason")
        tp_score = (shout_dbg or {}).get("two_phase_score")
        vosk_model = (shout_dbg or {}).get("vosk_model")
        main_whisper_model = str(getattr(self.rec, "model_size", ""))
        main_vosk_model = str(getattr(self.rec, "vosk_model_name", "") or getattr(self.rec, "vosk_model_path", ""))
        main_model = main_whisper_model if backend == "whisper" else main_vosk_model
        err = (command_dbg or {}).get("error") or (shout_dbg or {}).get("error")

        detail_parts = [
            f"backend={backend}",
            f"main_model={main_model}",
            f"reason={reason}",
        ]
        if attempted:
            detail_parts.append(f"attempted={','.join(attempted)}")
        if recognized_text:
            detail_parts.append(f"recognized_text={json.dumps(recognized_text, ensure_ascii=False)}")
        if vosk_model is not None:
            detail_parts.append(f"voice-commands_model={vosk_model}")
        if tp_reason is not None:
            detail_parts.append(f"two_phase_reason={tp_reason}")
        if tp_score is not None:
            detail_parts.append(f"two_phase_score={tp_score}")
        if utt is not None:
            detail_parts.append(f"utt_sec={utt}")
        if tsil is not None:
            detail_parts.append(f"tail_sil_ms={tsil}")
        if err:
            detail_parts.append(f"error={err}")
        return detail_parts, reason

    def _log_command_no_match_details(
        self,
        vad_stats: dict | None,
        attempted: list[str],
        shout_dbg: dict | None,
        command_dbg: dict | None,
    ) -> str:
        detail_parts, reason = self._build_command_no_match_detail_parts(vad_stats, attempted, shout_dbg, command_dbg)
        detail_line = " ".join(detail_parts)
        grammar_parts = self._attempted_command_grammar_parts(attempted)
        if grammar_parts:
            detail_line = detail_line + " " + " ".join(grammar_parts)
        print(detail_line, flush=True)
        return reason

    def _maybe_log_command_no_match(
        self,
        vad_stats: dict | None,
        *,
        attempted_categories: list[str] | None = None,
        shout_dbg: dict | None = None,
        command_dbg: dict | None = None,
    ) -> None:
        now = time.perf_counter()
        if now - self.last_command_no_match_ts <= 1.0:
            return
        self.last_command_no_match_ts = now
        attempted = list(dict.fromkeys([str(v).strip().lower() for v in (attempted_categories or []) if str(v).strip()]))
        reason = str((command_dbg or {}).get("reason") or (shout_dbg or {}).get("reason") or "no_match")
        recognized_text = str((command_dbg or {}).get("text") or "").strip()
        if recognized_text and reason in ("no_match", "weak_overlap"):
            log_warn(f"[LISTEN][WARN] Voice Command unrecognized {json.dumps(recognized_text, ensure_ascii=False)}")
        else:
            log_warn("[LISTEN][WARN] Voice Command unrecognized")
        reason = self._log_command_no_match_details(vad_stats, attempted, shout_dbg, command_dbg)

        # In-game debug hint (shown by plugin as [DVC] ...)
        # Keep the main phrase stable for user-facing diagnostics.
        self._send_debug_notification("Command unrecognized")

        # If recognizer returned an explicit "empty" reason, show it too.
        if reason in ("empty_audio", "phase_a_grammar_empty"):
            self._send_debug_notification(f"Empty recognition: {reason}")

    def _handle_shout_recognition(self, pcm16) -> tuple[bool, dict | None]:
        _save_debug_wav("shout", pcm16, self.rec)
        shout_result, shout_dbg = self.rec.recognize_shout_debug(pcm16)
        if not shout_result:
            return False, shout_dbg
        plugin, baseid, power, score, raw_text = shout_result
        msg = f"TRIG|shout|{plugin}|{baseid}|{power}|{score:.3f}|{raw_text}"
        write_line(self.pipe, msg)
        print(f"[SHOUT] >>> {msg}", flush=True)
        self._send_debug_notification(
            f"Shout triggered: \"{self._format_trigger_name(raw_text)}\" power={power}"
        )
        self._log_listen_state("after TRIG|shout", force=True)
        return True, shout_dbg

    def _try_item_recognition(self, pcm16) -> tuple[bool, list[str]]:
        """Try recognizing weapons, spells, potions. Returns match flag and attempted categories."""
        attempted: list[str] = []
        for kind in ("weapon", "spell", "potion"):
            if not self.state.get(f"{kind}s_enable", False):
                continue
            method_name = f"recognize_{kind}"
            if not hasattr(self.rec, method_name):
                continue
            attempted.append(kind)
            result = getattr(self.rec, method_name)(pcm16)
            if result:
                formid_hex, score, raw_text = result
                msg = f"TRIG|{kind}|{formid_hex}|{score:.3f}|{raw_text}"
                write_line(self.pipe, msg)
                print(f"[{kind.upper()}] >>> {msg}", flush=True)
                action = {
                    "weapon": "Weapon equipped",
                    "spell": "Spell equipped",
                    "potion": "Potion used",
                }.get(kind)
                if action:
                    self._send_debug_notification(
                        f"{action}: \"{self._format_trigger_name(raw_text)}\""
                    )
                self._log_listen_state(f"after TRIG|{kind}", force=True)
                return True, attempted
        return False, attempted

    def _try_power_recognition(self, pcm16) -> tuple[bool, dict | None]:
        if not self.state.get("powers_enable", False):
            return False, None
        result, command_dbg = self.rec.recognize_power_debug(pcm16)
        if result:
            formid_hex, score, raw_text = result
            msg = f"TRIG|power|{formid_hex}|{score:.3f}|{raw_text}"
            write_line(self.pipe, msg)
            print(f"[POWER] >>> {msg}", flush=True)
            self._send_debug_notification(f"Power triggered: \"{self._format_trigger_name(raw_text)}\"")
            self._log_listen_state("after TRIG|power", force=True)
            return True, command_dbg
        return False, command_dbg

    def _try_whisper_non_shout_recognition(self, pcm16) -> tuple[bool, list[str], dict | None]:
        enabled_categories: list[str] = []
        if self.state.get("powers_enable", False):
            enabled_categories.append("power")
        for kind in ("weapon", "spell", "potion"):
            if self.state.get(f"{kind}s_enable", False):
                enabled_categories.append(kind)

        result, command_dbg = self.rec.recognize_non_shout_commands_debug(pcm16, enabled_categories)
        attempted = list((command_dbg or {}).get("attempted") or [])
        if not result:
            return False, attempted, command_dbg

        kind, formid_hex, score, raw_text = result
        msg = f"TRIG|{kind}|{formid_hex}|{score:.3f}|{raw_text}"
        write_line(self.pipe, msg)
        print(f"[{kind.upper()}] >>> {msg}", flush=True)
        action = {
            "power": "Power triggered",
            "weapon": "Weapon equipped",
            "spell": "Spell equipped",
            "potion": "Potion used",
        }.get(kind)
        if action:
            self._send_debug_notification(f"{action}: \"{self._format_trigger_name(raw_text)}\"")
        self._log_listen_state(f"after TRIG|{kind}", force=True)
        return True, attempted, command_dbg

    def _try_vosk_command_flow(self, pcm16) -> tuple[bool, list[str], dict | None, dict | None]:
        attempted_categories: list[str] = []
        shout_dbg = None

        if self.state.get("shouts_enable", False) and self.shout_context_allowed:
            attempted_categories.append("shout")
            matched, shout_dbg = self._handle_shout_recognition(pcm16)
            if matched:
                return True, attempted_categories, shout_dbg, None

        matched_power, command_dbg = self._try_power_recognition(pcm16)
        if command_dbg is not None:
            attempted_categories.append("power")
        if matched_power:
            return True, attempted_categories, shout_dbg, command_dbg

        matched_item, item_attempted = self._try_item_recognition(pcm16)
        attempted_categories.extend(item_attempted)
        return matched_item, attempted_categories, shout_dbg, command_dbg

    def _try_whisper_command_flow(self, pcm16) -> tuple[bool, list[str], dict | None, dict | None]:
        matched_non_shout, attempted_categories, command_dbg = self._try_whisper_non_shout_recognition(pcm16)
        if matched_non_shout:
            return True, attempted_categories, None, command_dbg

        shout_dbg = None
        if self.state.get("shouts_enable", False) and self.shout_context_allowed:
            attempted_categories.append("shout")
            matched, shout_dbg = self._handle_shout_recognition(pcm16)
            if matched:
                return True, attempted_categories, shout_dbg, command_dbg

        return False, attempted_categories, shout_dbg, command_dbg

    def _try_command_capture(self, pcm16) -> tuple[bool, list[str], dict | None, dict | None]:
        if self.rec.uses_whisper_for_commands():
            return self._try_whisper_command_flow(pcm16)
        return self._try_vosk_command_flow(pcm16)

    def _handle_open_recognition(self, pcm16) -> None:
        _save_debug_wav("open", pcm16, self.rec)
        text, _ = self.rec.transcribe_open(pcm16)
        matched, score, phrase = matching.match_open(text)

        if phrase:
            print(f"[LISTEN] heard: \"{text}\" -> matched={matched}, score={score:.3f}, phrase=\"{phrase}\"", flush=True)
        else:
            print(f"[LISTEN] heard: \"{text}\" -> matched={matched}, score={score:.3f}", flush=True)

        if matched:
            write_dbg_line(self.pipe, f'Recognition: "{text}"')
            write_dbg_line(self.pipe, f'Dialogue Open: "{phrase}" score={score:.3f}')
            write_line(self.pipe, f"TRIG|open|{score:.3f}|{text}")
            print(f"[LISTEN] >>> TRIG|open|{score:.3f}|{text}", flush=True)
            self.listen_mode = False
            self.listen_commands_before_dialog = self.listen_commands
            self.listen_commands = False
            self.await_dialog_open_until = time.perf_counter() + 3.0
            self._log_listen_state("after TRIG|open", force=True)

    def _awaiting_dialog_open_without_pipe_data(self) -> bool:
        return (
            self.await_dialog_open_until
            and (time.perf_counter() < self.await_dialog_open_until)
            and (not self._has_pending_data())
        )

    def _can_listen_now(self) -> bool:
        open_enabled = self._open_listen_active()
        commands_active = self._commands_listen_active()
        shouts_enabled = self.state["shouts_enable"] and commands_active and self.shout_context_allowed
        powers_enabled = self.state.get("powers_enable", False) and commands_active
        items_enabled = self._any_items_enabled() and commands_active
        return bool(open_enabled or shouts_enabled or powers_enabled or items_enabled)

    def _drain_non_dialog_pipe_commands(self) -> None:
        while self._has_pending_data() and (not self.dialog_mode):
            if not self._handle_non_dialog_line(self._next_line()):
                break

    def _process_listen_capture(self, pcm16, vad_stats) -> None:
        if self._open_priority_active():
            self._handle_open_recognition(pcm16)
            return

        commands_active = self._commands_listen_active()
        if commands_active:
            matched, attempted_categories, shout_dbg, command_dbg = self._try_command_capture(pcm16)
            if matched:
                return
            if attempted_categories:
                self._maybe_log_command_no_match(
                    vad_stats,
                    attempted_categories=attempted_categories,
                    shout_dbg=shout_dbg,
                    command_dbg=command_dbg,
                )
            return
        if self._open_listen_active():
            self._handle_open_recognition(pcm16)

    def _any_items_enabled(self) -> bool:
        return bool(
            self.state.get("weapons_enable", False)
            or self.state.get("spells_enable", False)
            or self.state.get("potions_enable", False)
        )

    def _any_command_enabled(self) -> bool:
        return bool(
            self.state.get("shouts_enable", False)
            or self.state.get("powers_enable", False)
            or self._any_items_enabled()
        )

    def _run_listen_iteration(self) -> None:
        if self._awaiting_dialog_open_without_pipe_data():
            time.sleep(0.01)
            return

        if not self._can_listen_now():
            self.listen_mode = False
            self._log_listen_state("auto idle (nothing enabled)")
            time.sleep(0.05)
            return

        self._drain_non_dialog_pipe_commands()

        if self.dialog_mode or self._has_pending_data():
            return

        pcm16, vad_stats, cap_reason = self.audio.capture_for_open()
        if cap_reason == "pipe" or pcm16 is None:
            return

        self._process_listen_capture(pcm16, vad_stats)

    def _capture_dialog_audio(self):
        pcm16, vad_stats, cap_reason = self.audio.capture_for_dialogue()
        if cap_reason == "pipe":
            return None, None
        if pcm16 is None:
            if cap_reason == "no_hotkey":
                time.sleep(0.01)
            elif cap_reason == "no_speech":
                '''print(f"VAD: no speech (wait={vad_stats['t_wait']:.3f}s)", flush=True)'''
            return None, None
        return pcm16, vad_stats

    def _recognize_dialog_with_fallback(self, pcm16):
        text, asr_stats = self.rec.transcribe_dialogue(pcm16)
        t_m0 = time.perf_counter()
        scores = matching.rank_dialogue_options(text, self.options)
        idx0, sc1 = matching.best_dialogue_option(text, self.options)
        if self.dialog_grammar_json and self.rec.asr_engine == "vosk" and idx0 < 0:
            print("[DIALOG] no confident match with grammar, fallback to free ASR", flush=True)
            text, asr_stats = self.rec.transcribe_dialogue_free(pcm16)
            scores = matching.rank_dialogue_options(text, self.options)
            idx0, sc1 = matching.best_dialogue_option(text, self.options)
        t_m1 = time.perf_counter()
        return text, asr_stats, scores, idx0, sc1, (t_m1 - t_m0)

    def _send_dialog_result(self, idx0: int, sc1: float, close_hit: bool):
        t_s0 = time.perf_counter()
        if close_hit:
            write_line(self.pipe, "RES|-2|1.0")
            res_str = "RES|-2"
        elif idx0 >= 0:
            write_line(self.pipe, f"RES|{idx0}|{sc1:.3f}")
            res_str = f"RES|{idx0}|{sc1:.3f}"
        else:
            write_line(self.pipe, "RES|-1|0.0")
            res_str = "RES|-1"
        t_s1 = time.perf_counter()
        return res_str, (t_s1 - t_s0)

    def _run_dialog_iteration(self) -> None:
        if self._has_pending_data() and self._handle_dialog_line(self._rl()):
            return

        t_total0 = time.perf_counter()
        pcm16, vad_stats = self._capture_dialog_audio()
        if pcm16 is None:
            return

        _save_debug_wav("dialogue", pcm16, self.rec)

        text, asr_stats, scores, idx0, sc1, t_match = self._recognize_dialog_with_fallback(pcm16)

        close_hit, close_score, close_phrase = (False, 0.0, "")
        if self.state["close_enable_voice"]:
            close_hit, close_score, close_phrase = matching.match_close(text)
            if close_hit:
                _save_debug_wav("close", pcm16, self.rec)

        close_selected = bool(close_hit and idx0 < 0)

        if close_selected:
            write_dbg_line(self.pipe, f'Recognition: "{text}"')
            write_dbg_line(self.pipe, f'Dialogue Close: "{close_phrase}" score={close_score:.3f}')
        elif idx0 >= 0:
            write_dbg_line(self.pipe, f'Recognition: "{text}"')
            write_dbg_line(self.pipe, f'Dialogue pick {idx0} score={sc1:.3f}')

        res_str, t_send = self._send_dialog_result(idx0, sc1, close_selected)
        t_total1 = time.perf_counter()

        print("\n--- RECOGNIZED ---")
        print(text)
        if close_selected:
            print(f"[CLOSE-ASR] \"{text}\" (phrase=\"{close_phrase}\" score={close_score:.3f})")
        elif close_hit and idx0 >= 0:
            print(
                f"[CLOSE-IGNORED] \"{text}\" (phrase=\"{close_phrase}\" score={close_score:.3f}) "
                f"because dialog option idx0={idx0} score={sc1:.3f}",
                flush=True,
            )

        print("--- TIMINGS ---")
        print(f"utt_sec={vad_stats['utt_sec']:.2f}s  tail_sil={vad_stats['tail_sil_ms']:.0f}ms")
        print(f"t_wait={vad_stats['t_wait']:.3f}s  t_capture={vad_stats['t_vad']:.3f}s")
        print(f"t_wav={asr_stats.get('t_wav', 0.0):.3f}s  t_asr={asr_stats.get('t_asr', asr_stats.get('t_whisper', 0.0)):.3f}s")
        print(f"t_match={t_match:.4f}s  t_send={t_send:.4f}s")
        print(f"t_total={(t_total1 - t_total0):.3f}s  -> {res_str}")

        print("--- TOP ---")
        for rank, (sc, idx, opt) in enumerate(scores[:3], 1):
            print(f"{rank}. idx0={idx} score={sc:.3f} | {opt}")

    def run(self) -> None:
        while True:
            if (self.listen_mode or self.listen_commands) and not self.dialog_mode:
                self._run_listen_iteration()
                continue
            if self.dialog_mode:
                self._run_dialog_iteration()
                continue
            self._handle_idle_line(self._next_line())


def _warmup_runtime(audio: AudioPipeline, rec: Recognizer, shouts_enable: bool) -> None:
    parts: list[str] = [f"ASR.Engine={rec.asr_engine}"]

    if rec.asr_engine == "whisper":
        parts.append(f"Whisper.Language={rec.asr_lang}")
        parts.append(
            f"Whisper.Backend={_env_str('DVC_BACKEND', str(rec.cfg.backend))}"
        )
        parts.append(f"Whisper.Model={rec.model_size}")
        parts.append(f"Whisper.BeamSize={rec.whisper_beam}")
        parts.append(f"Whisper.InMemAudio={int(rec.use_inmem_audio)}")
    elif rec.asr_engine == "vosk":
        parts.append(f"Vosk.Model={rec.vosk_model_name}")

    parts.append(f"Mode.Mode={audio.mode}")

    print(f"[RUN] {' | '.join(parts)}", flush=True)

    if rec.asr_engine == "whisper":
        print("Loading Whisper model…", flush=True)
        rec.warmup()
        print(f"Whisper loaded ({rec.device}, {rec.compute_type})", flush=True)
    elif rec.asr_engine == "vosk":
        print(f"Loading Vosk model… ({rec.vosk_model_name or rec.vosk_model_path})", flush=True)
        rec.warmup()
        print("Vosk loaded", flush=True)
    else:
        raise RuntimeError(f"Unknown ASR engine: {rec.asr_engine}")

    if audio.mode == "vad":
        print("Loading Silero VAD (torch hub)…", flush=True)
        audio.warmup()
        print("VAD loaded", flush=True)

    if shouts_enable:
        print("[SHOUT] Voice shouts enabled, warming up…", flush=True)
        if not rec.warmup_shouts():
            log_warn("[SHOUT][WARN] Shout recognition warmup skipped/failed")


def main(cfg=None):
    cfg = cfg if cfg is not None else ServerConfig()
    open_enable_open = _env_bool("DVC_OPEN_ENABLE_OPEN", bool(cfg.open_enable_open))
    shouts_enable = _env_bool("DVC_SHOUTS_ENABLE", bool(cfg.shouts_enable))
    close_enable_voice = _env_bool("DVC_CLOSE_ENABLE_VOICE", bool(cfg.close_enable_voice))

    audio = AudioPipeline(cfg)
    matching.init(cfg)
    print(f"Current device input: {audio.active_input_device_label()}(Wrong? Run check_audio_device.bat and change setmic= in DVCRuntime.ini)", flush=True)
    print("PIPE server starting:", PIPE_NAME, flush=True)

    while True:
        pipe = win32pipe.CreateNamedPipe(
            PIPE_NAME,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1, 65536, 65536, 0, None,
        )

    
        try:
            _connect_with_wait(pipe)
        except pywintypes.error:
            continue

        addr = PIPE_NAME
        # Ensure the live "Waiting for client" line ends with a raw newline
        # — print via the same Rich Console so the live render is properly terminated
        _WAIT_CONSOLE.print("")
        log_success(f"Client connected: {addr}")

        reader = PipeReader()
        try:
            lang_key, lang_label, prefetch = _await_game_language(pipe, reader)
        except Exception as e:
            log_error(f"[GAME][ERR] failed to receive game language: {e}")
            try:
                win32file.CloseHandle(pipe)
            except Exception:
                pass
            continue

        _apply_language_to_cfg(cfg, lang_key)
        os.environ["DVC_VOSK_MODEL"] = str(cfg.vosk_model or "")
        os.environ["DVC_SHOUTS_VOSK_MODEL"] = str(cfg.shouts_vosk_model or "")
        os.environ["DVC_SHOUTS_LANG"] = str(cfg.shouts_language or "")
        os.environ["DVC_ASR_LANG"] = str(cfg.asr_lang or "")

        if cfg.asr_engine == "vosk":
            try:
                _ensure_main_vosk_model(cfg)
            except Exception as e:
                log_error(f"[VOSK][FATAL] {e}")
                raise

        rec = Recognizer(cfg)
        _warmup_runtime(audio, rec, shouts_enable)

        session = ClientSession(
            pipe=pipe,
            audio=audio,
            rec=rec,
            open_enable_open=open_enable_open,
            shouts_enable=shouts_enable,
            close_enable_voice=close_enable_voice,
            prefetch_lines=prefetch,
            reader=reader,
        )

        try:
            session.run()
        except Exception as e:
            log_error(f"Client disconnected: {e}")
        finally:
            try:
                win32file.CloseHandle(pipe)
            except Exception:
                pass

        print("Client session ended, exiting server", flush=True)
        return

if __name__ == "__main__":
    main()