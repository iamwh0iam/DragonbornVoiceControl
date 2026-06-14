from dataclasses import dataclass, field
from contextlib import suppress
import configparser
from pathlib import Path
from typing import Any, Callable

@dataclass
class ServerConfig:
    log_level: str = "debug"
    log_level_invalid: bool = False

    asr_engine: str = "vosk"
    asr_lang: str = "en"
    asr_lang_specified: bool = False
    vosk_model: str = ""

    backend: str = "cpu"
    mode: str = "vad"
    cuda: str = "cu128"
    cuda_specified: bool = False
    whisper_model: str = "base"
    whisper_device: str = "auto"
    whisper_compute: str = "auto"
    whisper_beam: int = 5
    whisper_command_beam: int = 1
    whisper_command_best_of: int = 1
    whisper_command_temperature: float = 0.0
    whisper_command_log_prob_threshold: float = -0.85
    whisper_command_no_speech_threshold: float = 0.6
    whisper_command_compression_ratio_threshold: float = 1.8
    whisper_command_repetition_penalty: float = 1.15
    whisper_command_no_repeat_ngram_size: int = 2
    whisper_command_max_new_tokens: int = 12
    whisper_command_max_words: int = 5
    whisper_command_word_slack: int = 1
    whisper_command_hotwords: bool = True
    whisper_command_fuzzy_match: bool = True

    dialogue_select_score_thr: float = 0.5
    dialogue_select_min_diff: float = 0.12

    vad_frame: int = 512
    vad_start_ms: int = 180
    vad_end_sil_ms: int = 350
    vad_max_utt: float = 4.5
    vad_min_utt: float = 0.45
    vad_max_wait: float = 6.0
    vad_thr: float = 0.5
    vad_preroll_ms: int = 260

    ptt_key: str = "f2"
    ptt_sec: float | None = None
    SetMic: str = ""
    pause_phrases: str = "Please don't listen to me, Stop speech recognition"
    resume_phrases: str = "Please listen to me again, Start speech recognition"

    inmem_audio: bool = True

    # Open phrases (voice-triggered dialogue open)
    open_phrases: str = ""
    open_score_thr: float = 0.4
    open_max_rec_sec: float = 1.5
    open_vad_end_sil_ms: int = 250

    # Close phrases (voice-triggered dialogue close)
    close_phrases: str = ""
    close_score_thr: float = 0.5

    # Voice-triggered dialog controls
    open_enable_open: bool = True
    close_enable_voice: bool = True

    # Voice equip controls
    voice_equip_specify_hand: bool = False
    voice_equip_right_hand_suffix: str = "right"
    voice_equip_left_hand_suffix: str = "left"
    voice_equip_both_hands_suffix: str = "both"
    voice_equip_quick_equip: bool = True
    voice_equip_equipment_types: str = "Dagger, Mace, Sword, Axe, Battleaxe, Battle axe, Greatsword, Warhammer, Bow, Crossbow, Shield"

    # Potion quick-use controls
    potions_quick_use: bool = True
    potions_health: str = "Healing potion, Health potion"
    potions_magicka: str = "Magicka potion, Mana potion"
    potions_stamina: str = "Stamina potion"
    potions_best_potion: bool = True

    # Shouts voice recognition
    shouts_enable: bool = False
    shouts_backend: str = "vosk"
    shouts_language: str = ""
    shouts_vosk_model: str = ""

    # Voice-triggered custom console/key commands
    custom_commands: dict[str, list[str]] = field(default_factory=dict)


_MISSING = object()


_TEXT = lambda v: v.strip()
_TEXT_LOWER = lambda v: v.strip().lower()
_IDENTITY = lambda v: v


def _normalize_log_level(value: str) -> str | None:
    normalized = str(value).strip().lower()
    if normalized == "warn":
        return "warning"
    if normalized in ("debug", "info", "warning"):
        return normalized
    return None


def _read_ini_text(ini: Path) -> str:
    try:
        return Path(ini).read_text(encoding="utf-8")
    except Exception:
        return ""


def _split_top_level_config(text: str) -> tuple[str, bool, str]:
    parser_lines: list[str] = []
    log_level = "debug"
    invalid = False
    in_top_level = True

    for line in text.splitlines():
        stripped = line.strip()
        if in_top_level and stripped.startswith("["):
            in_top_level = False

        if in_top_level:
            if not stripped or stripped.startswith("#") or stripped.startswith(";"):
                continue
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip().lower() != "loglevel":
                continue
            parsed = _normalize_log_level(value)
            if parsed is None:
                invalid = True
                log_level = "debug"
            else:
                log_level = parsed
            continue

        parser_lines.append(line)

    return "\n".join(parser_lines), invalid, log_level


_READERS: dict[str, Callable[[configparser.ConfigParser, str, str], Any]] = {
    "get": lambda cfg, section, option: cfg.get(section, option),
    "getint": lambda cfg, section, option: cfg.getint(section, option),
    "getfloat": lambda cfg, section, option: cfg.getfloat(section, option),
    "getboolean": lambda cfg, section, option: cfg.getboolean(section, option),
}


_LOAD_RULES = (
    ("asr_engine", "get", _TEXT_LOWER, (("ASR", "Engine"),)),
    ("asr_lang", "get", _TEXT_LOWER, (("Whisper", "Language"),)),
    ("vosk_model", "get", _TEXT, (("Vosk", "Model"),)),
    ("backend", "get", _TEXT_LOWER, (("Whisper", "Backend"),)),
    ("cuda", "get", _TEXT, (("Whisper", "Cuda"),)),
    ("mode", "get", _TEXT_LOWER, (("Voice Mode", "Mode"),)),
    ("whisper_model", "get", _TEXT, (("Whisper", "Model"),)),
    ("whisper_device", "get", _TEXT_LOWER, (("Whisper", "Device"),)),
    ("whisper_compute", "get", _TEXT_LOWER, (("Whisper", "ComputeType"),)),
    ("whisper_beam", "getint", _IDENTITY, (("Whisper", "BeamSize"),)),
    ("whisper_command_beam", "getint", _IDENTITY, (("Whisper", "CommandBeamSize"),)),
    ("whisper_command_best_of", "getint", _IDENTITY, (("Whisper", "CommandBestOf"),)),
    ("whisper_command_temperature", "getfloat", _IDENTITY, (("Whisper", "CommandTemperature"),)),
    ("whisper_command_log_prob_threshold", "getfloat", _IDENTITY, (("Whisper", "CommandLogProbThreshold"),)),
    ("whisper_command_no_speech_threshold", "getfloat", _IDENTITY, (("Whisper", "CommandNoSpeechThreshold"),)),
    ("whisper_command_compression_ratio_threshold", "getfloat", _IDENTITY, (("Whisper", "CommandCompressionRatioThreshold"),)),
    ("whisper_command_repetition_penalty", "getfloat", _IDENTITY, (("Whisper", "CommandRepetitionPenalty"),)),
    ("whisper_command_no_repeat_ngram_size", "getint", _IDENTITY, (("Whisper", "CommandNoRepeatNgramSize"),)),
    ("whisper_command_max_new_tokens", "getint", _IDENTITY, (("Whisper", "CommandMaxNewTokens"),)),
    ("whisper_command_max_words", "getint", _IDENTITY, (("Whisper", "CommandMaxWords"),)),
    ("whisper_command_word_slack", "getint", _IDENTITY, (("Whisper", "CommandWordSlack"),)),
    ("whisper_command_hotwords", "getboolean", _IDENTITY, (("Whisper", "CommandHints"),)),
    ("whisper_command_fuzzy_match", "getboolean", _IDENTITY, (("Whisper", "CommandSimilarMatch"),)),
    ("dialogue_select_score_thr", "getfloat", _IDENTITY, (("Dialogue Select", "ScoreThreshold"),)),
    ("dialogue_select_min_diff", "getfloat", _IDENTITY, (("Dialogue Select", "MinDiff"),)),
    ("vad_frame", "getint", _IDENTITY, (("VAD", "Frame"),)),
    ("vad_start_ms", "getint", _IDENTITY, (("VAD", "StartMs"),)),
    ("vad_end_sil_ms", "getint", _IDENTITY, (("VAD", "EndSilenceMs"),)),
    ("vad_max_utt", "getfloat", _IDENTITY, (("VAD", "MaxUttSec"),)),
    ("vad_min_utt", "getfloat", _IDENTITY, (("VAD", "MinUttSec"),)),
    ("vad_max_wait", "getfloat", _IDENTITY, (("VAD", "MaxWaitSec"),)),
    ("vad_thr", "getfloat", _IDENTITY, (("VAD", "Threshold"),)),
    ("vad_preroll_ms", "getint", _IDENTITY, (("VAD", "PreRollMs"),)),
    ("ptt_key", "get", _TEXT_LOWER, (("Voice Mode", "Hotkey"),)),
    ("ptt_sec", "getfloat", _IDENTITY, (("Voice Mode", "Seconds"),)),
    ("SetMic", "get", _TEXT, (("Voice Mode", "SetMic"),)),
    ("pause_phrases", "get", _TEXT, (("Voice Mode", "PausePhrases"),)),
    ("resume_phrases", "get", _TEXT, (("Voice Mode", "ResumePhrases"),)),
    ("inmem_audio", "getboolean", _IDENTITY, (("Whisper", "InMemAudio"),)),
    ("open_phrases", "get", _TEXT, (("Dialogue Open", "OpenPhrases"),)),
    ("open_score_thr", "getfloat", _IDENTITY, (("Dialogue Open", "ScoreThreshold"),)),
    ("open_max_rec_sec", "getfloat", _IDENTITY, (("Dialogue Open", "MaxRecordSec"),)),
    ("open_vad_end_sil_ms", "getint", _IDENTITY, (("Dialogue Open", "VadEndSilenceMs"),)),
    ("close_phrases", "get", _TEXT, (("Dialogue Close", "ClosePhrases"),)),
    ("close_score_thr", "getfloat", _IDENTITY, (("Dialogue Close", "ScoreThreshold"),)),
    ("open_enable_open", "getboolean", _IDENTITY, (("Dialogue Open", "EnableVoiceOpen"),)),
    ("close_enable_voice", "getboolean", _IDENTITY, (("Dialogue Close", "EnableVoiceClose"),)),
    ("voice_equip_right_hand_suffix", "get", _TEXT, (("Equip", "RightHandSuffix"),)),
    ("voice_equip_left_hand_suffix", "get", _TEXT, (("Equip", "LeftHandSuffix"),)),
    ("voice_equip_both_hands_suffix", "get", _TEXT, (("Equip", "BothHandsSuffix"),)),
    ("voice_equip_equipment_types", "get", _TEXT, (("Equip", "EquipmentTypes"),)),
    ("potions_health", "get", _TEXT, (("Potions", "Health"),)),
    ("potions_magicka", "get", _TEXT, (("Potions", "Magicka"),)),
    ("potions_stamina", "get", _TEXT, (("Potions", "Stamina"),)),
    ("shouts_enable", "getboolean", _IDENTITY, (("Shouts", "EnableVoiceShouts"),)),
    ("shouts_backend", "get", _TEXT_LOWER, (("Shouts", "VoiceShoutsBackend"),)),
    ("shouts_language", "get", _TEXT_LOWER, (("Shouts", "Language"),)),
    ("shouts_vosk_model", "get", _TEXT, (("Shouts", "Model"),)),
)


def _pick(cfg: configparser.ConfigParser, getter: str, sources):
    read = _READERS[getter]
    for section, option in sources:
        with suppress(configparser.NoOptionError, configparser.NoSectionError, ValueError):
            return read(cfg, section, option)
    return _MISSING


def _read_custom_commands(cfg: configparser.ConfigParser) -> dict[str, list[str]]:
    section = "Custom Commands"
    if not cfg.has_section(section):
        return {}

    commands: dict[str, list[str]] = {}
    for phrase, raw_value in cfg.items(section):
        phrase = str(phrase or "").strip()
        values = [part.strip() for part in str(raw_value or "").split(";")]
        values = [part for part in values if part]
        if phrase and values:
            commands[phrase] = values
    return commands

def load_config(ini: Path) -> ServerConfig:
    c = ServerConfig()

    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    with suppress(TypeError):
        ini_path = Path(ini)
        if ini_path.exists():
            parser_text, invalid_log_level, log_level = _split_top_level_config(_read_ini_text(ini_path))
            c.log_level = log_level
            c.log_level_invalid = invalid_log_level
            with suppress(configparser.Error):
                cfg.read_string(parser_text)

    for attr, getter, transform, sources in _LOAD_RULES:
        value = _pick(cfg, getter, sources)
        if value is _MISSING:
            continue
        if attr == "asr_lang":
            setattr(c, attr, transform(value))
            c.asr_lang_specified = True
            continue
        if attr == "cuda":
            setattr(c, attr, transform(value))
            c.cuda_specified = True
            continue
        setattr(c, attr, transform(value))

    c.custom_commands = _read_custom_commands(cfg)
    return c
