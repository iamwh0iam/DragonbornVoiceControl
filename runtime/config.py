from dataclasses import dataclass
from contextlib import suppress
import configparser
from pathlib import Path
from typing import Any, Callable

@dataclass
class ServerConfig:
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

    min_score: float = 0.5
    min_diff: float = 0.12

    vad_frame: int = 512
    vad_start_ms: int = 180
    vad_end_sil_ms: int = 350
    vad_max_utt: float = 4.5
    vad_min_utt: float = 0.45
    vad_max_wait: float = 6.0
    vad_thr: float = 0.5
    vad_preroll_ms: int = 260

    ptt_key: str = "f2"
    ptt_sec: float = 3.0
    SetMic: str = ""

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

    # Shouts voice recognition
    shouts_enable: bool = False
    shouts_backend: str = "vosk"
    shouts_language: str = ""
    shouts_vosk_model: str = ""


_MISSING = object()


_TEXT = lambda v: v.strip()
_TEXT_LOWER = lambda v: v.strip().lower()
_IDENTITY = lambda v: v


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
    ("mode", "get", _TEXT_LOWER, (("Mode", "Mode"),)),
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
    ("min_score", "getfloat", _IDENTITY, (("Matching", "MinScore"),)),
    ("min_diff", "getfloat", _IDENTITY, (("Matching", "MinDiff"),)),
    ("vad_frame", "getint", _IDENTITY, (("VAD", "Frame"),)),
    ("vad_start_ms", "getint", _IDENTITY, (("VAD", "StartMs"),)),
    ("vad_end_sil_ms", "getint", _IDENTITY, (("VAD", "EndSilenceMs"),)),
    ("vad_max_utt", "getfloat", _IDENTITY, (("VAD", "MaxUttSec"),)),
    ("vad_min_utt", "getfloat", _IDENTITY, (("VAD", "MinUttSec"),)),
    ("vad_max_wait", "getfloat", _IDENTITY, (("VAD", "MaxWaitSec"),)),
    ("vad_thr", "getfloat", _IDENTITY, (("VAD", "Threshold"),)),
    ("vad_preroll_ms", "getint", _IDENTITY, (("VAD", "PreRollMs"),)),
    ("ptt_key", "get", _TEXT_LOWER, (("PTT", "Hotkey"),)),
    ("ptt_sec", "getfloat", _IDENTITY, (("PTT", "Seconds"),)),
    ("SetMic", "get", _TEXT, (("Mode", "SetMic"),)),
    ("inmem_audio", "getboolean", _IDENTITY, (("Whisper", "InMemAudio"),)),
    ("open_phrases", "get", _TEXT, (("Open", "OpenPhrases"),)),
    ("open_score_thr", "getfloat", _IDENTITY, (("Open", "ScoreThreshold"),)),
    ("open_max_rec_sec", "getfloat", _IDENTITY, (("Open", "MaxRecordSec"),)),
    ("open_vad_end_sil_ms", "getint", _IDENTITY, (("Open", "VadEndSilenceMs"),)),
    ("close_phrases", "get", _TEXT, (("Close", "ClosePhrases"),)),
    ("close_score_thr", "getfloat", _IDENTITY, (("Close", "ScoreThreshold"),)),
    ("open_enable_open", "getboolean", _IDENTITY, (("Open", "EnableVoiceOpen"),)),
    ("close_enable_voice", "getboolean", _IDENTITY, (("Close", "EnableVoiceClose"),)),
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

def load_config(ini: Path) -> ServerConfig:
    c = ServerConfig()

    cfg = configparser.ConfigParser()
    with suppress(TypeError):
        ini_path = Path(ini)
        ini_path.exists() and cfg.read(ini_path, encoding="utf-8")

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

    return c