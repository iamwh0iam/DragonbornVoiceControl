import base64
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
from log_utils import setup_timestamped_print, log_debug, log_info, log_warn, log_error, log_success

setup_timestamped_print()

def _app_dir() -> Path:
    raw = os.environ.get("DVC_APP_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(sys.executable).resolve().parent if bool(getattr(sys, "frozen", False)) else Path(__file__).resolve().parent


# ===== PIPE =====
PIPE_NAME = r"\\.\pipe\DVC_voice_local"

CMD_OPEN_PREFIX = "OPEN|"
CMD_CLOSE = "CLOSE"
CMD_LISTEN_ON = "LISTEN|1"
CMD_LISTEN_OFF = "LISTEN|0"
CMD_LANG_PREFIX = "LANG|"

SR = 16000
ITEM_HAND_VALUE_SEP = "\t"


def _split_item_match(kind: str, value: str) -> tuple[str, str]:
    if kind not in ("weapon", "spell"):
        return str(value or ""), ""

    formid, sep, hand = str(value or "").partition(ITEM_HAND_VALUE_SEP)
    hand = hand.strip().lower() if sep else "right"
    if hand not in ("left", "right", "both"):
        hand = "right"
    return formid, hand


def _item_trigger_message(kind: str, formid_hex: str, hand: str, score: float, raw_text: str) -> str:
    if kind in ("weapon", "spell"):
        return f"TRIG|{kind}|{formid_hex}|{hand}|{score:.3f}|{raw_text}"
    return f"TRIG|{kind}|{formid_hex}|{score:.3f}|{raw_text}"


def _custom_trigger_message(commands_json: str, score: float, raw_text: str) -> str:
    payload = base64.urlsafe_b64encode(str(commands_json or "[]").encode("utf-8")).decode("ascii")
    payload = payload.rstrip("=")
    return f"TRIG|custom|{score:.3f}|{raw_text}|{payload}"


def _log_quote(value) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _fmt_score(value) -> str:
    try:
        return f"{float(value):.3f}"
    except Exception:
        return "0.000"


_QUOTED_LOG_FIELDS = {"candidate", "phrase", "option", "name", "target", "recognized_text", "raw_text", "matched_phrase", "error"}


def _fmt_field_value(key: str, value) -> str:
    if isinstance(value, str):
        return _log_quote(value) if key in _QUOTED_LOG_FIELDS else value
    if isinstance(value, float):
        return _fmt_score(value)
    return str(value)


def _append_field(parts: list[str], key: str, value) -> None:
    if value is None or value == "":
        return
    parts.append(f"{key}={_fmt_field_value(key, value)}")


def _log_line_parts(prefix: str, **fields) -> str:
    parts = [prefix]
    for key, value in fields.items():
        _append_field(parts, key, value)
    return " ".join(parts)


def _emit_pipe_log(pipe, level: str, line: str) -> None:
    if pipe is not None:
        write_dbg_line(pipe, f"LOG|{level.upper()}|{line}")
    if level.upper() == "WARN":
        log_warn(line)
    elif level.upper() == "DEBUG":
        log_debug(line)
    else:
        log_info(line)


def _asr_lat_field(stats: dict | None):
    if not isinstance(stats, dict):
        return None
    for key in ("t_asr", "t_whisper"):
        try:
            value = stats.get(key)
            if value is not None:
                return float(value)
        except Exception:
            pass
    return None


def _formid_key(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return f"0x{int(raw, 16):x}"
    except Exception:
        return raw.lower()


def _log_voice_command_trigger(kind: str, raw_text: str, score: float, formid: str, **fields) -> None:
    parts = [f"Voice command recognized={_log_quote(raw_text)}"]
    asr_lat = fields.pop("asr_lat", None)
    _append_field(parts, "asr_lat", asr_lat)
    _append_field(parts, "kind", kind)
    _append_field(parts, "score", float(score))
    match_result = fields.pop("match_result", None)
    matched_phrase = fields.pop("matched_phrase", None)
    _append_field(parts, "match_result", match_result)
    _append_field(parts, "matched_phrase", matched_phrase)
    _append_field(parts, "formid", formid)
    for key, value in fields.items():
        _append_field(parts, key, value)
    parts.append("action=trigger")
    log_success(" ".join(parts))


def _log_custom_voice_command_trigger(raw_text: str, score: float, meta: dict | None = None, asr_lat=None) -> None:
    parts = [f"Voice command recognized={_log_quote(raw_text)}"]
    _append_field(parts, "asr_lat", asr_lat)
    _append_field(parts, "kind", "custom")
    _append_field(parts, "result", "accepted")
    _append_field(parts, "score", float(score))
    if isinstance(meta, dict) and meta.get("result") == "fuzzy":
        _append_field(parts, "match_result", "fuzzy")
        _append_field(parts, "matched_phrase", meta.get("matched_phrase") or meta.get("phrase"))
    log_success(" ".join(parts))


def _log_voice_command_quick_equip(raw_text: str, score: float, formid: str, **fields) -> None:
    parts = [f"Voice command recognized={_log_quote(raw_text)}"]
    asr_lat = fields.pop("asr_lat", None)
    _append_field(parts, "asr_lat", asr_lat)
    _append_field(parts, "result", "quick_equip")
    _append_field(parts, "kind", "weapon")
    _append_field(parts, "score", float(score))
    _append_field(parts, "formid", formid)
    for key, value in fields.items():
        _append_field(parts, key, value)
    parts.append("action=trigger")
    log_success(" ".join(parts))


def _log_item_voice_command_trigger(kind: str, raw_text: str, score: float, formid: str, hand: str, name: str, meta: dict | None = None, asr_lat=None) -> None:
    if kind == "weapon" and isinstance(meta, dict) and meta.get("result") == "quick_equip":
        _log_voice_command_quick_equip(raw_text, score, formid, hand=hand, name=name, asr_lat=asr_lat)
        return

    fields = {
        "hand": hand,
        "name": name,
        "asr_lat": asr_lat,
    }
    if isinstance(meta, dict) and meta.get("result") == "fuzzy":
        fields["match_result"] = "fuzzy"
        fields["matched_phrase"] = meta.get("matched_phrase") or meta.get("phrase")

    _log_voice_command_trigger(
        kind,
        raw_text,
        score,
        formid,
        **fields,
    )


def _item_notification_text(kind: str, raw_text: str, name: str, meta: dict | None = None) -> str:
    if kind == "weapon" and isinstance(meta, dict) and meta.get("result") == "quick_equip":
        resolved = str(name or "").strip()
        if resolved:
            return resolved
    if kind == "potion" and isinstance(meta, dict) and meta.get("result") == "quick_use":
        resolved = str(name or "").strip()
        if resolved:
            return resolved
    return raw_text


def _log_voice_command_state(text: str, result: str, score: float, **fields) -> None:
    parts = [f"Voice command recognized={_log_quote(text)}"]
    asr_lat = fields.pop("asr_lat", None)
    _append_field(parts, "asr_lat", asr_lat)
    _append_field(parts, "result", result)
    action = fields.pop("action", None)
    for key, value in fields.items():
        _append_field(parts, key, value)
    _append_field(parts, "score", float(score))
    _append_field(parts, "action", action)
    line = " ".join(parts)
    if result == "accepted":
        log_success(line)
    elif result == "ignored":
        log_warn(line)
    else:
        log_debug(line)


def _log_dialogue_open(text: str, score: float, matched: bool, phrase: str | None = None, asr_lat=None) -> None:
    parts = [f"Dialogue command recognized={_log_quote(text)}"]
    _append_field(parts, "asr_lat", asr_lat)
    _append_field(parts, "result", "open" if matched else "no_match")
    if matched:
        if phrase:
            parts.append(f"phrase={_log_quote(phrase)}")
        parts.append(f"score={_fmt_score(score)}")
        parts.append("action=trigger")
        log_success(" ".join(parts))
        return

    reason = "empty_text" if not matching.normalize(text) else "no_candidate"
    fields: dict = {"reason": reason}
    if phrase and score > 0.0:
        fields["reason"] = "below_threshold"
        fields["candidate"] = f"open : {phrase}"
        fields["threshold"] = matching.open_score_threshold()
    for key, value in fields.items():
        _append_field(parts, key, value)
    parts.append(f"score={_fmt_score(score)}")
    log_warn(" ".join(parts))


def _log_dialogue_command(text: str, result: str, score: float, **fields) -> None:
    parts = [f"Dialogue command recognized={_log_quote(text)}"]
    asr_lat = fields.pop("asr_lat", None)
    _append_field(parts, "asr_lat", asr_lat)
    _append_field(parts, "result", result)
    for key, value in fields.items():
        _append_field(parts, key, value)
    _append_field(parts, "score", float(score))
    line = " ".join(parts)
    if result == "no_match":
        log_warn(line)
    else:
        log_success(line)


def _dialogue_command_line(text: str, result: str, score: float, **fields) -> str:
    parts = [f"Dialogue command recognized={_log_quote(text)}"]
    asr_lat = fields.pop("asr_lat", None)
    _append_field(parts, "asr_lat", asr_lat)
    _append_field(parts, "result", result)
    for key, value in fields.items():
        _append_field(parts, key, value)
    _append_field(parts, "score", float(score))
    return " ".join(parts)

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
            log_info(f"[GAME] game language detected: {lang_label}")
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
        log_info(f"[WAV] save_wav=on record_saved={rel_path}")
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
        path = _app_dir() / "shouts_map.json"
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
        "POTIONS_QUICK_USE": ("potions_quick_use", "DVC_POTIONS_QUICK_USE"),
        "POTIONS_BEST_POTION": ("potions_best_potion", "DVC_POTIONS_BEST_POTION"),
        "SPECIFY_HAND": ("voice_equip_specify_hand", "DVC_VOICE_EQUIP_SPECIFY_HAND"),
        "QUICK_EQUIP": ("voice_equip_quick_equip", "DVC_VOICE_EQUIP_QUICK_EQUIP"),
        "KEY_CONSOLE": ("key_console_enable", "DVC_KEY_CONSOLE_ENABLE"),
        "PAUSE_RESUME": ("pause_resume_enable", "DVC_PAUSE_RESUME_ENABLE"),
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

    rec_attrs = {
        "POTIONS_QUICK_USE": "potions_quick_use",
        "POTIONS_BEST_POTION": "potions_best_potion",
        "SPECIFY_HAND": "voice_equip_specify_hand",
        "QUICK_EQUIP": "voice_equip_quick_equip",
    }
    rec_attr = rec_attrs.get(kind)
    if rec_attr:
        try:
            setattr(rec, rec_attr, bool(enabled))
        except Exception as e:
            log_warn(f"[CFG][WARN] failed to apply {kind} to recognizer: {e}")

    rebuild_targets = {
        "POTIONS_QUICK_USE": ("potions",),
        "POTIONS_BEST_POTION": ("potions",),
        "QUICK_EQUIP": ("weapons",),
        "SPECIFY_HAND": ("weapons", "spells"),
    }
    rebuild = rebuild_targets.get(kind)
    if rebuild:
        try:
            if "weapons" in rebuild:
                rec.set_allowed_weapons_entries(getattr(rec, "_allowed_weapon_entries", None))
            if "spells" in rebuild:
                rec.set_allowed_spells_entries(getattr(rec, "_allowed_spell_entries", None))
            if "potions" in rebuild:
                rec.set_allowed_potions_entries(getattr(rec, "_allowed_potion_entries", None))
        except Exception as e:
            log_warn(f"[CFG][WARN] failed to rebuild recognizer after {kind}: {e}")

    if kind == "SHOUTS" and enabled:
        try:
            if not rec.warmup_shouts():
                log_warn("[SHOUT][WARN] warmup skipped/failed after CFG|SHOUTS|1")
        except Exception as e:
            log_warn(f"[SHOUT][WARN] warmup failed after CFG|SHOUTS|1: {e}")

    return (kind, enabled)


def _try_read_state_packet(line: str) -> tuple | None:
    if not isinstance(line, str) or not line.startswith("STATE|"):
        return None

    parts = line.split("|")
    if parts[1].strip().upper() == "ALL" and len(parts) in (5, 6):
        return (
            parts[2].strip().lower() in ("1", "true", "yes", "on"),
            parts[3].strip().lower() in ("1", "true", "yes", "on"),
            parts[4].strip().lower() in ("1", "true", "yes", "on"),
            parts[5].strip().lower() in ("1", "true", "yes", "on") if len(parts) == 6 else False,
        )
    return None


def _print_dialog_options(
    options: list[str],
    dialog_grammar_json: str | None,
    close_grammar_json: str | None,
    audio: AudioPipeline,
    listen_enabled: bool = True,
) -> None:
    log_info("\n[DIALOG OPENED] OPTIONS:")
    for i, o in enumerate(options, 1):
        log_info(f" {i}. {o}")
    if dialog_grammar_json:
        log_debug(f"[OPTIONS GRAMMAR] {dialog_grammar_json}")
    if close_grammar_json:
        log_info(f"[CLOSE GRAMMAR] {close_grammar_json}")
    if not listen_enabled:
        return
    if audio.mode == "ptt":
        log_info(f"\nPTT: hold {audio.hotkey.upper()} and speak…")
    else:
        log_info("\nVAD: Listening... (speak, the recording will start automatically)")


def _print_dialog_update(
    options: list[str],
    dialog_grammar_json: str | None,
    close_grammar_json: str | None,
) -> None:
    log_info("\n[DIALOG UPDATED] OPTIONS:")
    for i, o in enumerate(options, 1):
        log_info(f" {i}. {o}")
    if dialog_grammar_json:
        log_debug(f"[OPTIONS GRAMMAR] {dialog_grammar_json}")
    if close_grammar_json:
        log_info(f"[CLOSE GRAMMAR] {close_grammar_json}")


def _print_dialog_closed() -> None:
    log_debug("[DIALOG] CLOSED")
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
        self.commands_paused = False
        self.shout_context_allowed = False
        self.player_drawn = False
        self.player_combat = False
        self.menu_blocked = False
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
        self._pending_listen_update = False
        self.voice_state = VoiceState()
        self._shout_id_to_name: dict[str, str] = {}
        self._shout_formid_to_name: dict[str, str] = {}
        self._voice_command_names: dict[str, dict[str, str]] = {
            "power": {},
            "weapon": {},
            "spell": {},
            "potion": {},
        }
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
            "key_console_enable": False,
            "pause_resume_enable": False,
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
            "key_console": bool(self._custom_commands_enabled()),
        }

    def _open_listen_active(self) -> bool:
        return bool(
            self.listen_mode
            and self.state.get("open_enable_open", False)
            and (not self.player_combat)
        )

    def _open_priority_active(self) -> bool:
        return bool(self._open_listen_active() and (not self.player_drawn))

    def _commands_listen_active(self) -> bool:
        return bool(
            self.listen_mode
            and self._voice_command_features_available()
            and (not self.commands_paused)
            and (not self._open_priority_active())
        )

    def _pause_resume_enabled(self) -> bool:
        return bool(self.state.get("pause_resume_enable", False))

    def _custom_commands_enabled(self) -> bool:
        return bool(self.state.get("key_console_enable", False) and self.rec.has_custom_commands())

    def _pause_resume_listen_active(self) -> bool:
        return bool(self._pause_resume_enabled() and (self.commands_paused or self.listen_mode))

    def _dialog_voice_active(self) -> bool:
        return bool(
            self.state.get("dialogue_select_enable", False)
            or self.state.get("close_enable_voice", False)
            or self._pause_resume_listen_active()
        )

    def _dialog_options_log_active(self) -> bool:
        return bool(
            self.state.get("dialogue_select_enable", False)
            or self.state.get("close_enable_voice", False)
        )

    def _effective_state(self) -> dict[str, bool]:
        self.voice_state.set_feature_enabled(self._feature_enabled_snapshot())
        self.voice_state.set_dialog_open(self.dialog_mode)
        self.voice_state.set_open_listen(self._open_listen_active())
        self.voice_state.set_commands_listen(self._commands_listen_active())
        self.voice_state.set_shout_context_allowed(self.shout_context_allowed)
        self.voice_state.set_pause_resume(self._pause_resume_enabled(), self.commands_paused)
        return self.voice_state.effective()

    def _format_listen_update(self, effective: dict[str, bool]) -> str:
        flag = lambda value: "ON" if value else "OFF"
        return (
            f"[LISTEN] update State: "
            f"listen={flag(self.listen_mode)} "
            f"commands={flag(self._commands_listen_active())} "
            f"menu={flag(self.menu_blocked)} "
            f"paused={flag(self.commands_paused)} "
            f"dialog={flag(self.dialog_mode)} "
            f"drawn={flag(self.player_drawn)} "
            f"combat={flag(self.player_combat)} | "
            f"{self.voice_state.format_effective(effective)}"
        )

    def _listen_state_snapshot(self, effective: dict[str, bool]) -> tuple:
        return (
            bool(self.listen_mode),
            bool(self._commands_listen_active()),
            bool(self.menu_blocked),
            bool(self.shout_context_allowed),
            bool(self.player_drawn),
            bool(self.player_combat),
            bool(self.dialog_mode),
            tuple(int(effective.get(k, False)) for k in effective.keys()),
        )

    def _emit_listen_state(self, effective: dict[str, bool], *, force: bool = False) -> None:
        snap = self._listen_state_snapshot(effective)
        if (not force) and (snap == self.last_listen_state):
            return
        self.last_listen_state = snap
        log_info(self._format_listen_update(effective))

    def _send_effective_if_changed(self, *, reason: str | None = None) -> None:
        self.voice_state.set_feature_enabled(self._feature_enabled_snapshot())
        self.voice_state.set_dialog_open(self.dialog_mode)
        self.voice_state.set_open_listen(self._open_listen_active())
        self.voice_state.set_commands_listen(self._commands_listen_active())
        self.voice_state.set_shout_context_allowed(self.shout_context_allowed)
        self.voice_state.set_pause_resume(self._pause_resume_enabled(), self.commands_paused)
        _changed, effective = self.voice_state.effective_changed()
        self._emit_listen_state(effective)

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

    def _send_forced_debug_notification(self, text: str) -> None:
        write_dbg_line(self.pipe, text)

    def _format_trigger_name(self, raw_text: str | None) -> str:
        name = (raw_text or "").strip()
        return name if name else "?"

    def _rl(self):
        return self.read_line(self.pipe)

    def _queue_cfg_log(self, reason: str, kind: str, enabled: bool) -> None:
        if self._pending_cfg_reason and self._pending_cfg_reason != reason:
            self._flush_listen_update(force=True)
        if not self._pending_cfg_reason:
            self._pending_cfg_reason = reason
        self._pending_cfg_log.append(f"{kind}={1 if enabled else 0}")

    def _flush_listen_update(self, *, force: bool = False) -> None:
        if not self._pending_cfg_log and not self._pending_listen_update:
            return
        self._pending_cfg_log = []
        self._pending_cfg_reason = None
        if self._pending_listen_update:
            self._pending_listen_update = False
            self._send_effective_if_changed()

    def _merge_pending_cfg_into_next_update(self) -> None:
        self._pending_cfg_log = []
        self._pending_cfg_reason = None
        self._pending_listen_update = False

    def _log_listen_state(self, reason: str, *, force: bool = False) -> None:
        effective = self._effective_state()
        self._emit_listen_state(effective, force=force)

    def _reset_dialog_state(self) -> None:
        self.dialog_mode = False
        self.dialog_grammar_phrases = []
        self.dialog_grammar_json = None
        self.close_grammar_phrases = []
        self.close_grammar_json = None
        self.rec.clear_dialog_grammar()

    def _refresh_dialog_grammar(self) -> None:
        grammar_groups: list[list[str]] = []
        if self.state.get("dialogue_select_enable", False):
            grammar_groups.append(self.dialog_grammar_phrases)
        if self.state.get("close_enable_voice", False):
            grammar_groups.append(self.close_grammar_phrases)
        self.rec.set_dialog_grammar(_merge_grammar(*grammar_groups))

    def _open_dialog(self, new_options: list[str], *, reason: str) -> None:
        self.options = new_options
        self.dialog_grammar_phrases, self.dialog_grammar_json = _dialog_grammar(self.options)
        self.close_grammar_phrases, self.close_grammar_json = _close_grammar()
        self._refresh_dialog_grammar()
        self.dialog_mode = True
        self.listen_mode = False
        self._send_effective_if_changed(reason=reason)
        if self._dialog_options_log_active():
            _print_dialog_options(
                self.options,
                self.dialog_grammar_json if self.state.get("dialogue_select_enable", False) else None,
                self.close_grammar_json if self.state.get("close_enable_voice", False) else None,
                self.audio,
                listen_enabled=self._dialog_voice_active(),
            )

    def _update_dialog(self, new_options: list[str]) -> None:
        self.options = new_options
        self.dialog_grammar_phrases, self.dialog_grammar_json = _dialog_grammar(self.options)
        self.close_grammar_phrases, self.close_grammar_json = _close_grammar()
        self._refresh_dialog_grammar()
        if self._dialog_options_log_active():
            _print_dialog_update(
                self.options,
                self.dialog_grammar_json if self.state.get("dialogue_select_enable", False) else None,
                self.close_grammar_json if self.state.get("close_enable_voice", False) else None,
            )

    def _close_dialog(self, *, reason: str) -> None:
        _print_dialog_closed()
        self._reset_dialog_state()
        self._merge_pending_cfg_into_next_update()
        if not self._has_pending_data():
            self._send_effective_if_changed(reason=reason)

    def _handle_cfg_or_state(self, line: str, *, reason: str) -> bool:
        cfg = _try_read_cfg_packet(line, state=self.state, rec=self.rec)
        if cfg is not None:
            kind, enabled = cfg
            if kind == "PAUSE_RESUME" and not enabled:
                self.commands_paused = False
            if self.dialog_mode and kind in ("DIALOGUE_SELECT", "CLOSE"):
                self._refresh_dialog_grammar()
            self._queue_cfg_log(reason, kind, enabled)
            self._pending_listen_update = True
            if not self._has_pending_data():
                self._flush_listen_update(force=True)
            return True

        state_update = _try_read_state_packet(line)
        if state_update is not None:
            self.shout_context_allowed = bool(state_update[0])
            self.player_drawn = bool(state_update[1])
            self.player_combat = bool(state_update[2])
            self.menu_blocked = bool(state_update[3]) if len(state_update) > 3 else False
            self._pending_listen_update = True
            if not self._has_pending_data():
                self._flush_listen_update(force=True)
            return True

        return False

    def _parse_favorite_packet_line(
        self,
        line: str,
        shouts: list[tuple[str, str, str, str]],
        powers: list[tuple[str, str]],
        weapons: list[tuple[str, str]],
        spells: list[tuple[str, str]],
        potions: list[tuple],
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
            if len(parts) >= 7:
                potions.append((parts[2], parts[3], parts[4], parts[5], parts[6]))

    def _collect_favorites_payload(self) -> tuple[
        list[tuple[str, str, str, str]],
        list[tuple[str, str]],
        list[tuple[str, str]],
        list[tuple[str, str]],
        list[tuple],
    ]:
        shouts: list[tuple[str, str, str, str]] = []
        powers: list[tuple[str, str]] = []
        weapons: list[tuple[str, str]] = []
        spells: list[tuple[str, str]] = []
        potions: list[tuple] = []

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
        self._shout_formid_to_name = {}
        for entry in shouts:
            if len(entry) < 4:
                continue
            formid = _formid_key(entry[1])
            name = str(entry[2] or "").strip()
            editor_id = _normalize_shout_id(entry[3])
            if editor_id and name:
                self._shout_id_to_name[editor_id] = name
            if formid and name:
                self._shout_formid_to_name[formid] = name

    def _update_voice_command_name_maps(
        self,
        powers: list[tuple[str, str]],
        weapons: list[tuple[str, str]],
        spells: list[tuple[str, str]],
        potions: list[tuple],
    ) -> None:
        entries_by_kind = {
            "power": powers,
            "weapon": weapons,
            "spell": spells,
            "potion": potions,
        }
        self._voice_command_names = {}
        for kind, entries in entries_by_kind.items():
            names: dict[str, str] = {}
            for entry in entries:
                if len(entry) < 2:
                    continue
                formid, name = entry[0], entry[1]
                key = _formid_key(formid)
                value = str(name or "").strip()
                if key and value:
                    names[key] = value
            self._voice_command_names[kind] = names

    def _voice_command_name(self, kind: str, formid: str) -> str:
        if kind == "shout":
            return self._shout_formid_to_name.get(_formid_key(formid), "")
        return self._voice_command_names.get(kind, {}).get(_formid_key(formid), "")

    def _log_favorites_state(
        self,
        shouts: list[tuple[str, str, str, str]],
        powers: list[tuple[str, str]],
        weapons: list[tuple[str, str]],
        spells: list[tuple[str, str]],
        potions: list[tuple],
    ) -> None:
        log_info(
            "[FAVORITES][STATE] Fav updated: "
            f"shouts [{self._format_favorite_names(self._favorite_names(shouts, 2))}], "
            f"powers [{self._format_favorite_names(self._favorite_names(powers, 1))}], "
            f"weapons [{self._format_favorite_names(self._favorite_names(weapons, 1))}], "
            f"spells [{self._format_favorite_names(self._favorite_names(spells, 1))}], "
            f"potions [{self._format_favorite_names(self._favorite_names(potions, 1))}]"
        )

    def _voice_command_features_available(self) -> bool:
        return bool(
            (self.state.get("shouts_enable", False) and self.rec.has_command_category("shout"))
            or (self.state.get("powers_enable", False) and self.rec.has_command_category("power"))
            or (self.state.get("weapons_enable", False) and self.rec.has_command_category("weapon"))
            or (self.state.get("spells_enable", False) and self.rec.has_command_category("spell"))
            or (self.state.get("potions_enable", False) and self.rec.has_command_category("potion"))
            or (self._custom_commands_enabled() and self.rec.has_command_category("custom"))
            or (self._pause_resume_enabled() and self.rec.has_command_category("pause"))
        )

    def _set_idle_listen_mode(self) -> None:
        self._merge_pending_cfg_into_next_update()
        self.listen_mode = True
        self._send_effective_if_changed(reason="LISTEN ON (idle)")

    def _handle_favorites_packet(self, line: str) -> bool:
        if line != "FAV|BEGIN":
            return False

        shouts, powers, weapons, spells, potions = self._collect_favorites_payload()
        has_payload = bool(shouts or powers or weapons or spells or potions)
        if (not self._favorites_features_enabled()) and (not has_payload):
            return True

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
        self._update_voice_command_name_maps(powers, weapons, spells, potions)
        self._log_favorites_state(shouts, powers, weapons, spells, potions)
        self._send_effective_if_changed(reason="favorites update")

        if self.state.get("debug_enabled", False):
            self._send_debug_notification("Fav updated")

        return True

    def _handle_non_dialog_line(self, line: str) -> bool:
        if self._handle_favorites_packet(line):
            return True
        if self._handle_cfg_or_state(line, reason="cfg update"):
            return True
        if line == CMD_CLOSE:
            self._reset_dialog_state()
            self._merge_pending_cfg_into_next_update()
            self._send_effective_if_changed(reason="dialog CLOSE")
            return True
        if line == CMD_LISTEN_OFF:
            self.listen_mode = False
            self._merge_pending_cfg_into_next_update()
            self._send_effective_if_changed(reason="LISTEN OFF")
            return True
        if line == CMD_LISTEN_ON:
            self.listen_mode = True
            self._merge_pending_cfg_into_next_update()
            self._send_effective_if_changed(reason="LISTEN ON")
            return True
        if line.startswith(CMD_OPEN_PREFIX):
            self._open_dialog(read_open_packet(self.read_line, self.pipe), reason="dialog OPEN")
            return False
        self._flush_listen_update(force=True)
        return True

    def _handle_dialog_line(self, line: str) -> bool:
        if self._handle_favorites_packet(line):
            return True
        if self._handle_cfg_or_state(line, reason="cfg update (dialog)"):
            return True
        if line.startswith(CMD_OPEN_PREFIX):
            self._update_dialog(read_open_packet(self.read_line, self.pipe))
            return True
        if line == CMD_CLOSE:
            self._close_dialog(reason="dialog CLOSE")
            return True
        if line == CMD_LISTEN_ON:
            self._reset_dialog_state()
            self.listen_mode = True
            self._merge_pending_cfg_into_next_update()
            if not self._has_pending_data():
                self._send_effective_if_changed(reason="dialog CLOSE")
            return True
        if line == CMD_LISTEN_OFF:
            self.listen_mode = False
            self._merge_pending_cfg_into_next_update()
            self._send_effective_if_changed(reason="LISTEN OFF")
            return True
        self._flush_listen_update(force=True)
        return False

    def _handle_idle_line(self, line: str) -> None:
        if self._handle_favorites_packet(line):
            return
        if self._handle_cfg_or_state(line, reason="cfg update (idle)"):
            return
        if line.startswith(CMD_OPEN_PREFIX):
            self._open_dialog(read_open_packet(self.read_line, self.pipe), reason="dialog OPEN (idle)")
            return
        if line == CMD_LISTEN_ON:
            self._set_idle_listen_mode()
            return
        if line == CMD_LISTEN_OFF:
            self.listen_mode = False
            self._merge_pending_cfg_into_next_update()
            self._send_effective_if_changed(reason="LISTEN OFF (idle)")
            return
        self._flush_listen_update(force=True)

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
        if tag == "SHOUT":
            header = f"grammar_entries={entries} phrases={phrases}{lang_part}"
            return self._format_shout_grammar_block(header, shout_detail)

        header = f"phrases={phrases}{lang_part}"
        label = {
            "HAND_SUFFIXES": "HandSuffixes",
            "QUICK_EQUIP": "QuickEquip",
            "QUICK_USE": "QuickUse",
        }.get(tag, tag.capitalize() + "s")
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

        def _append_hand_suffixes_if_available() -> None:
            hs_entries, hs_phrases, hs_list = self.rec.get_hand_suffix_grammar_info()
            _append_if_available("HAND_SUFFIXES", hs_entries, hs_phrases, hs_list)

        attempted_set = set(attempted)
        grammar_parts: list[str] = []

        if "shout" in attempted_set:
            sh_entries, sh_phrases, sh_list, sh_lang, sh_detail = self.rec.get_shout_grammar_info()
            _append_if_available("SHOUT", sh_entries, sh_phrases, sh_list, sh_lang, shout_detail=sh_detail)

        if "spell" in attempted_set:
            sp_entries, sp_phrases, sp_list = self.rec.get_spell_grammar_info()
            _append_if_available("SPELL", sp_entries, sp_phrases, sp_list)
            if "weapon" not in attempted_set:
                _append_hand_suffixes_if_available()

        if "power" in attempted_set:
            pw_entries, pw_phrases, pw_list = self.rec.get_power_grammar_info()
            _append_if_available("POWER", pw_entries, pw_phrases, pw_list)

        if "weapon" in attempted_set:
            we_entries, we_phrases, we_list = self.rec.get_weapon_grammar_info()
            _append_if_available("WEAPON", we_entries, we_phrases, we_list)
            _append_hand_suffixes_if_available()
            qe_entries, qe_phrases, qe_list = self.rec.get_weapon_quick_grammar_info()
            _append_if_available("QUICK_EQUIP", qe_entries, qe_phrases, qe_list)

        if "potion" in attempted_set:
            po_entries, po_phrases, po_list = self.rec.get_potion_grammar_info()
            _append_if_available("POTION", po_entries, po_phrases, po_list)
            qu_entries, qu_phrases, qu_list = self.rec.get_potion_quick_grammar_info()
            _append_if_available("QUICK_USE", qu_entries, qu_phrases, qu_list)

        if "pause" in attempted_set:
            ps_entries, ps_phrases, ps_list = self.rec.get_pause_grammar_info()
            _append_if_available("PAUSE", ps_entries, ps_phrases, ps_list)

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
        asr_lat = _asr_lat_field(command_dbg) or _asr_lat_field(shout_dbg)
        if asr_lat is not None:
            detail_parts.append(f"asr_lat={asr_lat:.3f}")
        if err:
            detail_parts.append(f"error={err}")
        return detail_parts, reason

    def _command_no_match_line(
        self,
        attempted: list[str],
        shout_dbg: dict | None,
        command_dbg: dict | None,
    ) -> str:
        command_score = float((command_dbg or {}).get("score") or 0.0) if isinstance(command_dbg, dict) else 0.0
        shout_score = float((shout_dbg or {}).get("score") or (shout_dbg or {}).get("two_phase_score") or 0.0) if isinstance(shout_dbg, dict) else 0.0
        if isinstance(shout_dbg, dict) and shout_score > command_score:
            dbg = shout_dbg
        else:
            dbg = command_dbg if isinstance(command_dbg, dict) and command_dbg else shout_dbg
        if not isinstance(dbg, dict):
            dbg = {}

        recognized_text = str(dbg.get("text") or "").strip()
        raw_text = str(dbg.get("raw_text") or "").strip()
        reason = str(dbg.get("reason") or "no_match")
        candidates = dbg.get("candidates")
        fields: dict = {
            "asr_lat": _asr_lat_field(dbg),
            "result": "no_match",
            "reason": reason,
            "candidate": dbg.get("candidate"),
        }
        if raw_text and raw_text != recognized_text:
            fields["raw_text"] = raw_text
        for key in ("two_phase_reason",):
            if key in dbg:
                fields[key] = dbg.get(key)

        line = _log_line_parts(f"Voice command recognized={_log_quote(recognized_text)}", **fields)
        if candidates:
            line = line + " " + str(candidates)
        return line

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
        log_debug(detail_line)
        return reason

    def _command_no_match_debug_notification(
        self,
        reason: str,
        shout_dbg: dict | None,
        command_dbg: dict | None,
    ) -> str:
        command_score = float((command_dbg or {}).get("score") or 0.0) if isinstance(command_dbg, dict) else 0.0
        shout_score = float((shout_dbg or {}).get("score") or (shout_dbg or {}).get("two_phase_score") or 0.0) if isinstance(shout_dbg, dict) else 0.0
        if isinstance(shout_dbg, dict) and shout_score > command_score:
            dbg = shout_dbg
        else:
            dbg = command_dbg if isinstance(command_dbg, dict) and command_dbg else shout_dbg
        if not isinstance(dbg, dict):
            dbg = {}

        recognized_text = str(dbg.get("text") or dbg.get("raw_text") or "").strip()
        if recognized_text:
            return f'Command unrecognized: "{recognized_text}"'
        return "Command unrecognized: empty_text"

    def _dialogue_no_match_debug_notification(self, text: str) -> str:
        recognized_text = str(text or "").strip()
        if recognized_text:
            return f'Command unrecognized: "{recognized_text}"'
        return "Command unrecognized: empty_text"

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
        line = self._command_no_match_line(attempted, shout_dbg, command_dbg)
        _emit_pipe_log(self.pipe, "WARN", line)
        reason = self._log_command_no_match_details(vad_stats, attempted, shout_dbg, command_dbg)

        # In-game debug hint (shown by plugin as [DVC] ...)
        self._send_forced_debug_notification(
            self._command_no_match_debug_notification(reason, shout_dbg, command_dbg)
        )

    def _handle_shout_recognition(self, pcm16) -> tuple[bool, dict | None]:
        _save_debug_wav("shout", pcm16, self.rec)
        shout_result, shout_dbg = self.rec.recognize_shout_debug(pcm16)
        if not shout_result:
            return False, shout_dbg
        plugin, baseid, power, score, raw_text = shout_result
        msg = f"TRIG|shout|{plugin}|{baseid}|{power}|{score:.3f}|{raw_text}"
        write_line(self.pipe, msg)
        _log_voice_command_trigger(
            "shout",
            raw_text,
            score,
            baseid,
            asr_lat=_asr_lat_field(shout_dbg),
            power=power,
            name=self._voice_command_name("shout", baseid),
        )
        self._send_debug_notification(
            f"Shout triggered: \"{self._format_trigger_name(raw_text)}\" power={power}"
        )
        self._log_listen_state("after TRIG|shout")
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
                match_value, score, raw_text = result[:3]
                meta = result[3] if len(result) > 3 and isinstance(result[3], dict) else {}
                formid_hex, hand = _split_item_match(kind, match_value)
                msg = _item_trigger_message(kind, formid_hex, hand, score, raw_text)
                write_line(self.pipe, msg)
                _log_item_voice_command_trigger(
                    kind,
                    raw_text,
                    score,
                    formid_hex,
                    hand=hand,
                    name=self._voice_command_name(kind, formid_hex),
                    meta=meta,
                )
                action = {
                    "weapon": "Weapon equipped",
                    "spell": "Spell equipped",
                    "potion": "Potion used",
                }.get(kind)
                if action:
                    self._send_debug_notification(
                        f"{action}: \"{_item_notification_text(kind, raw_text, self._voice_command_name(kind, formid_hex), meta)}\""
                    )
                self._log_listen_state(f"after TRIG|{kind}")
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
            _log_voice_command_trigger(
                "power",
                raw_text,
                score,
                formid_hex,
                asr_lat=_asr_lat_field(command_dbg),
                name=self._voice_command_name("power", formid_hex),
            )
            self._send_debug_notification(f"Power triggered: \"{self._format_trigger_name(raw_text)}\"")
            self._log_listen_state("after TRIG|power")
            return True, command_dbg
        return False, command_dbg

    def _try_non_shout_recognition(self, pcm16) -> tuple[bool, list[str], dict | None]:
        enabled_categories: list[str] = []
        if self.state.get("powers_enable", False) and self.rec.has_command_category("power"):
            enabled_categories.append("power")
        for kind in ("weapon", "spell", "potion"):
            if self.state.get(f"{kind}s_enable", False) and self.rec.has_command_category(kind):
                enabled_categories.append(kind)
        if self._custom_commands_enabled():
            enabled_categories.append("custom")
        if self._pause_resume_enabled() and self.rec.has_command_category("pause"):
            enabled_categories.append("pause")

        result, command_dbg = self.rec.recognize_non_shout_commands_debug(pcm16, enabled_categories)
        attempted = list((command_dbg or {}).get("attempted") or [])
        if not result:
            return False, attempted, command_dbg

        kind, match_value, score, raw_text = result[:4]
        meta = result[4] if len(result) > 4 and isinstance(result[4], dict) else {}
        if kind == "pause":
            self.commands_paused = True
            _log_voice_command_state(raw_text, "accepted", score, phrase=raw_text, asr_lat=_asr_lat_field(command_dbg), action="pause_commands")
            self._send_forced_debug_notification("Voice commands disabled")
            self._log_listen_state("after voice command paused")
            return True, attempted, command_dbg

        if kind == "custom":
            write_line(self.pipe, _custom_trigger_message(match_value, score, raw_text))
            _log_custom_voice_command_trigger(raw_text, score, meta=meta, asr_lat=_asr_lat_field(command_dbg))
            self._log_listen_state("after TRIG|custom")
            return True, attempted, command_dbg

        formid_hex, hand = _split_item_match(kind, match_value)
        msg = _item_trigger_message(kind, formid_hex, hand, score, raw_text)
        write_line(self.pipe, msg)
        _log_item_voice_command_trigger(
            kind,
            raw_text,
            score,
            formid_hex,
            hand=hand,
            name=self._voice_command_name(kind, formid_hex),
            meta=meta,
            asr_lat=_asr_lat_field(command_dbg),
        )
        action = {
            "power": "Power triggered",
            "weapon": "Weapon equipped",
            "spell": "Spell equipped",
            "potion": "Potion used",
        }.get(kind)
        if action:
            self._send_debug_notification(
                f"{action}: \"{_item_notification_text(kind, raw_text, self._voice_command_name(kind, formid_hex), meta)}\""
            )
        self._log_listen_state(f"after TRIG|{kind}")
        return True, attempted, command_dbg

    def _try_vosk_command_flow(self, pcm16) -> tuple[bool, list[str], dict | None, dict | None]:
        matched_non_shout, attempted_categories, command_dbg = self._try_non_shout_recognition(pcm16)
        if matched_non_shout:
            return True, attempted_categories, None, command_dbg

        if self.state.get("shouts_enable", False) and self.rec.has_command_category("shout") and self.shout_context_allowed:
            attempted_categories.append("shout")
            matched, shout_dbg = self._handle_shout_recognition(pcm16)
            if matched:
                return True, attempted_categories, shout_dbg, command_dbg
            return False, attempted_categories, shout_dbg, command_dbg

        return False, attempted_categories, None, command_dbg

    def _whisper_non_shout_should_block_shout(self, command_dbg: dict | None) -> bool:
        if not isinstance(command_dbg, dict):
            return False

        attempted = {str(v or "").strip().lower() for v in command_dbg.get("attempted") or []}
        if not attempted.intersection({"power", "weapon", "spell", "potion", "custom", "pause"}):
            return False

        raw_text = str(command_dbg.get("raw_text") or command_dbg.get("text") or "").strip()
        norm = matching.normalize(raw_text)
        if not norm:
            return False

        try:
            if float(command_dbg.get("score") or 0.0) >= 0.50:
                return True
        except Exception:
            pass

        if attempted.intersection({"weapon", "spell"}):
            text_tokens = matching.tokens(norm)
            suffixes = []
            for values in getattr(self.rec, "voice_equip_hand_suffixes", {}).values():
                suffixes.extend(values or [])
            for suffix in suffixes:
                suffix_tokens = matching.tokens(suffix)
                if suffix_tokens and len(text_tokens) > len(suffix_tokens) and text_tokens[-len(suffix_tokens):] == suffix_tokens:
                    return True

        return False

    def _try_whisper_command_flow(self, pcm16) -> tuple[bool, list[str], dict | None, dict | None]:
        matched_non_shout, attempted_categories, command_dbg = self._try_non_shout_recognition(pcm16)
        if matched_non_shout:
            return True, attempted_categories, None, command_dbg
        if self._whisper_non_shout_should_block_shout(command_dbg):
            return False, attempted_categories, None, command_dbg

        shout_dbg = None
        if self.state.get("shouts_enable", False) and self.rec.has_command_category("shout") and self.shout_context_allowed:
            attempted_categories.append("shout")
            matched, shout_dbg = self._handle_shout_recognition(pcm16)
            if matched:
                return True, attempted_categories, shout_dbg, command_dbg

        return False, attempted_categories, shout_dbg, command_dbg

    def _try_command_capture(self, pcm16) -> tuple[bool, list[str], dict | None, dict | None]:
        if self.rec.uses_whisper_for_commands():
            return self._try_whisper_command_flow(pcm16)
        return self._try_vosk_command_flow(pcm16)

    def _check_pause_resume_phrase(self, pcm16) -> tuple[bool, str, str]:
        if not self._pause_resume_enabled():
            return False, "", ""

        text, _stats = self.rec.transcribe_pause_resume(pcm16, paused=self.commands_paused)
        asr_lat = _asr_lat_field(_stats)
        if self.commands_paused:
            resume_hit, resume_score, resume_phrase, required, candidate = matching.match_resume(text)
            if resume_hit:
                self.commands_paused = False
                _log_voice_command_state(text, "accepted", resume_score, phrase=resume_phrase, asr_lat=asr_lat, action="resume_commands")
                self._send_forced_debug_notification("Voice commands enabled")
                self._log_listen_state("after voice command enabled")
                return True, "resumed", text
            if resume_score > 0.0:
                _log_voice_command_state(text, "no_match", resume_score, candidate=f"resume_commands : {candidate}", required=required, asr_lat=asr_lat)
            return True, "paused", text

        pause_hit, pause_score, pause_phrase, pause_required, pause_candidate = matching.match_pause(text)
        if pause_hit:
            self.commands_paused = True
            _log_voice_command_state(text, "accepted", pause_score, phrase=pause_phrase, asr_lat=asr_lat, action="pause_commands")
            self._send_forced_debug_notification("Voice commands disabled")
            self._log_listen_state("after voice command disabled")
            return True, "paused_now", text

        if pause_score > 0.0:
            _log_voice_command_state(text, "no_match", pause_score, candidate=f"pause_commands : {pause_candidate}", required=pause_required, asr_lat=asr_lat)
        return False, "", text

    def _handle_open_recognition(self, pcm16) -> None:
        _save_debug_wav("open", pcm16, self.rec)
        text, asr_stats = self.rec.transcribe_open(pcm16)
        asr_lat = _asr_lat_field(asr_stats)
        matched, score, phrase = matching.match_open(text)

        _log_dialogue_open(text, score, matched, phrase, asr_lat=asr_lat)

        if matched:
            write_dbg_line(self.pipe, f'Recognition: "{text}"')
            write_dbg_line(self.pipe, f'Dialogue Open: "{phrase}" score={score:.3f}')
            write_line(self.pipe, f"TRIG|open|{score:.3f}|{text}")
            self.listen_mode = False
            self.await_dialog_open_until = time.perf_counter() + 3.0
            self._log_listen_state("after TRIG|open")

    def _awaiting_dialog_open_without_pipe_data(self) -> bool:
        return (
            self.await_dialog_open_until
            and (time.perf_counter() < self.await_dialog_open_until)
            and (not self._has_pending_data())
        )

    def _can_listen_now(self) -> bool:
        pause_resume_enabled = self._pause_resume_listen_active()
        open_enabled = self._open_listen_active()
        commands_active = self._commands_listen_active()
        shouts_enabled = self.state["shouts_enable"] and self.rec.has_command_category("shout") and commands_active and self.shout_context_allowed
        powers_enabled = self.state.get("powers_enable", False) and self.rec.has_command_category("power") and commands_active
        items_enabled = self._any_items_ready() and commands_active
        custom_enabled = self._custom_commands_enabled() and commands_active
        return bool(pause_resume_enabled or open_enabled or shouts_enabled or powers_enabled or items_enabled or custom_enabled)

    def _command_listen_standby_active(self) -> bool:
        return bool(self.listen_mode and self._voice_command_features_available())

    def _drain_non_dialog_pipe_commands(self) -> None:
        while self._has_pending_data() and (not self.dialog_mode):
            if not self._handle_non_dialog_line(self._next_line()):
                break

    def _process_listen_capture(self, pcm16, vad_stats) -> None:
        if self.commands_paused:
            handled, state, text = self._check_pause_resume_phrase(pcm16)
            if handled:
                if state == "paused" and self.commands_paused:
                    _log_voice_command_state(text, "ignored", 0.0, reason="commands_paused", allowed="resume_only")
                return

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
            if self._open_listen_active():
                self._handle_open_recognition(pcm16)
            return
        if self._open_listen_active():
            self._handle_open_recognition(pcm16)
            return

        self._check_pause_resume_phrase(pcm16)

    def _any_items_enabled(self) -> bool:
        return bool(
            self.state.get("weapons_enable", False)
            or self.state.get("spells_enable", False)
            or self.state.get("potions_enable", False)
        )

    def _any_items_ready(self) -> bool:
        return bool(
            (self.state.get("weapons_enable", False) and self.rec.has_command_category("weapon"))
            or (self.state.get("spells_enable", False) and self.rec.has_command_category("spell"))
            or (self.state.get("potions_enable", False) and self.rec.has_command_category("potion"))
        )

    def _favorites_features_enabled(self) -> bool:
        return bool(
            self.state.get("shouts_enable", False)
            or self.state.get("powers_enable", False)
            or self._any_items_enabled()
        )

    def _any_command_enabled(self) -> bool:
        return bool(
            self.state.get("shouts_enable", False)
            or self.state.get("powers_enable", False)
            or self._any_items_enabled()
            or self._custom_commands_enabled()
            or self._pause_resume_enabled()
        )

    def _run_listen_iteration(self) -> None:
        if self._awaiting_dialog_open_without_pipe_data():
            time.sleep(0.01)
            return

        self._drain_non_dialog_pipe_commands()

        if self.dialog_mode or self._has_pending_data():
            return

        if not self._can_listen_now():
            if not self._command_listen_standby_active():
                self.listen_mode = False
                self._log_listen_state("auto idle (nothing enabled)")
            time.sleep(0.05)
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
                pass
            return None, None
        return pcm16, vad_stats

    def _recognize_dialog_with_fallback(self, pcm16):
        text, asr_stats = self.rec.transcribe_dialogue(pcm16)
        t_m0 = time.perf_counter()
        scores = matching.rank_dialogue_options(text, self.options)
        idx0, sc1 = matching.best_dialogue_option(text, self.options)
        if self.dialog_grammar_json and self.rec.asr_engine == "vosk" and idx0 < 0:
            log_debug("[DIALOG] no confident match with grammar, fallback to free ASR")
            text, asr_stats = self.rec.transcribe_dialogue_free(pcm16)
            scores = matching.rank_dialogue_options(text, self.options)
            idx0, sc1 = matching.best_dialogue_option(text, self.options)
        t_m1 = time.perf_counter()
        return text, asr_stats, scores, idx0, sc1, (t_m1 - t_m0)

    def _dialogue_no_match_fields(
        self,
        text: str,
        close_score: float,
        close_phrase: str,
        select_enabled: bool,
    ) -> tuple[float, dict] | None:
        close_diag = {
            "reason": "below_threshold" if close_score > 0.0 else "",
            "candidate": f"close : {close_phrase}" if close_score > 0.0 and close_phrase else "",
            "threshold": matching.close_score_threshold() if close_score > 0.0 else None,
        }

        close_diag_score = float(close_score or 0.0)
        if not select_enabled:
            return (close_diag_score, close_diag) if close_diag_score > 0.0 else None

        select_diag = matching.dialogue_match_diagnostics(text, self.options)
        select_score = float(select_diag.get("score") or 0.0)
        if close_diag_score > 0.0 and close_diag_score >= select_score:
            return close_diag_score, close_diag

        reason = str(select_diag.get("reason") or "no_match")
        fields = {
            "reason": reason,
            "candidate": f"select : {select_diag.get('option')}" if reason not in ("empty_text", "no_options") and select_diag.get("option") else "",
        }
        if reason == "below_threshold":
            fields["threshold"] = select_diag.get("threshold")
        elif reason == "ambiguous":
            fields["second_score"] = select_diag.get("second_score")
            fields["diff"] = select_diag.get("diff")
            fields["min_diff"] = select_diag.get("min_diff")
        return select_score, fields

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

        if not self._dialog_voice_active():
            self.rec.clear_dialog_grammar()
            time.sleep(0.05)
            return

        t_total0 = time.perf_counter()
        pcm16, vad_stats = self._capture_dialog_audio()
        if pcm16 is None:
            return

        _save_debug_wav("dialogue", pcm16, self.rec)

        select_enabled = bool(self.state.get("dialogue_select_enable", False))
        if select_enabled:
            text, asr_stats, scores, idx0, sc1, t_match = self._recognize_dialog_with_fallback(pcm16)
        else:
            text, asr_stats = self.rec.transcribe_dialogue(pcm16)
            scores = []
            idx0 = -1
            sc1 = 0.0
            t_match = 0.0

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
        asr_lat = _asr_lat_field(asr_stats)

        if close_selected:
            _log_dialogue_command(text, "close", close_score, asr_lat=asr_lat, phrase=close_phrase)
        elif idx0 >= 0:
            option = self.options[idx0] if 0 <= idx0 < len(self.options) else ""
            _log_dialogue_command(text, "select", sc1, asr_lat=asr_lat, option=option)
        else:
            no_match = self._dialogue_no_match_fields(
                text,
                close_score,
                close_phrase,
                select_enabled,
            )
            if no_match is not None:
                no_match_score, no_match_fields = no_match
                line = _dialogue_command_line(text, "no_match", no_match_score, asr_lat=asr_lat, **no_match_fields)
                _emit_pipe_log(self.pipe, "WARN", line)
                self._send_forced_debug_notification(
                    self._dialogue_no_match_debug_notification(text)
                )

        if close_selected:
            log_debug(f"[CLOSE-ASR] \"{text}\" (phrase=\"{close_phrase}\" score={close_score:.3f})")
        elif close_hit and idx0 >= 0:
            log_debug(
                f"[CLOSE-IGNORED] \"{text}\" (phrase=\"{close_phrase}\" score={close_score:.3f}) "
                f"because dialog option idx0={idx0} score={sc1:.3f}"
            )

        log_debug("--- TIMINGS ---")
        log_debug(f"utt_sec={vad_stats['utt_sec']:.2f}s  tail_sil={vad_stats['tail_sil_ms']:.0f}ms")
        log_debug(f"t_wait={vad_stats['t_wait']:.3f}s  t_capture={vad_stats['t_vad']:.3f}s")
        log_debug(f"t_wav={asr_stats.get('t_wav', 0.0):.3f}s  t_asr={asr_stats.get('t_asr', asr_stats.get('t_whisper', 0.0)):.3f}s")
        log_debug(f"t_match={t_match:.4f}s  t_send={t_send:.4f}s")
        log_debug(f"t_total={(t_total1 - t_total0):.3f}s  -> {res_str}")

        log_debug("--- TOP ---")
        for rank, (sc, idx, opt) in enumerate(scores[:3], 1):
            log_debug(f"{rank}. idx0={idx} score={sc:.3f} | {opt}")

    def run(self) -> None:
        while True:
            if self.listen_mode and not self.dialog_mode:
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

    parts.append(f"Voice Mode.Mode={audio.mode}")

    log_debug(f"[RUN] {' | '.join(parts)}")

    if rec.asr_engine == "whisper":
        log_info("Loading Whisper model…")
        rec.warmup()
        log_info(f"Whisper loaded ({rec.device}, {rec.compute_type})")
    elif rec.asr_engine == "vosk":
        log_info(f"Loading Vosk model… ({rec.vosk_model_name or rec.vosk_model_path})")
        rec.warmup()
        log_info("Vosk loaded")
    else:
        raise RuntimeError(f"Unknown ASR engine: {rec.asr_engine}")

    if audio.mode == "vad":
        log_debug("Loading Silero VAD (torch hub)…")
        audio.warmup()
        log_info("Silero VAD loaded")

    if shouts_enable:
        log_info("[SHOUT] Voice shouts enabled, warming up…")
        if not rec.warmup_shouts():
            log_warn("[SHOUT][WARN] Shout recognition warmup skipped/failed")


def main(cfg=None):
    cfg = cfg if cfg is not None else ServerConfig()
    open_enable_open = _env_bool("DVC_OPEN_ENABLE_OPEN", bool(cfg.open_enable_open))
    shouts_enable = _env_bool("DVC_SHOUTS_ENABLE", bool(cfg.shouts_enable))
    close_enable_voice = _env_bool("DVC_CLOSE_ENABLE_VOICE", bool(cfg.close_enable_voice))

    audio = AudioPipeline(cfg)
    matching.init(cfg)
    log_info(f"Current device input: {audio.active_input_device_label()}(Wrong? Run check_audio_device.bat and change setmic= in DVCRuntime.ini)")
    log_info(f"PIPE server starting: {PIPE_NAME}")

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

        log_info("Client Ready")
        write_line(pipe, "READY|CLIENT")

        try:
            session.run()
        except Exception as e:
            log_error(f"Client disconnected: {e}")
        finally:
            try:
                win32file.CloseHandle(pipe)
            except Exception:
                pass

        log_info("Client session ended, exiting server")
        return

if __name__ == "__main__":
    main()
