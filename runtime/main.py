import os
import sys
import traceback
import subprocess
from pathlib import Path

from rich.console import Console
from rich.rule import Rule
from rich.align import Align

# ---------------- paths ----------------
IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    RUNTIME_DIR = Path(sys.executable).resolve().parent
else:
    RUNTIME_DIR = Path(__file__).resolve().parent
MOD_DIR = RUNTIME_DIR.parent

PY_DIR = RUNTIME_DIR / "python312"
PY_EXE = PY_DIR / "python.exe"

CACHE_ROOT_ENV = os.environ.get("DVC_CACHE_DIR", "").strip()
CACHE_ROOT     = Path(CACHE_ROOT_ENV).expanduser().resolve() if CACHE_ROOT_ENV else RUNTIME_DIR
os.environ["DVC_CACHE_DIR"] = str(CACHE_ROOT)
CACHE          = CACHE_ROOT / "caches"
LOG_PATH_00   = RUNTIME_DIR / "dvc_server00.log"
LOG_PATH_01   = RUNTIME_DIR / "dvc_server01.log"

# ---------------- folders (create cache dirs prior to env setup) ----------------
(CACHE / "torch").mkdir(parents=True, exist_ok=True)
(CACHE / "hf").mkdir(parents=True, exist_ok=True)
(CACHE / "tmp").mkdir(parents=True, exist_ok=True)
(CACHE / "vosk").mkdir(parents=True, exist_ok=True)

# ---------------- strict isolation ----------------
os.environ["PYTHONNOUSERSITE"] = "1"
os.environ["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
os.environ["PIP_NO_PYTHON_VERSION_WARNING"] = "1"

os.environ["PYTHONPYCACHEPREFIX"] = str(CACHE / "pycache")

# ML caches
os.environ["TORCH_HOME"] = str(CACHE / "torch")
os.environ["HF_HOME"] = str(CACHE / "hf")
os.environ["HUGGINGFACE_HUB_CACHE"] = str(CACHE / "hf" / "hub")
os.environ["TMP"] = str(CACHE / "tmp")
os.environ["TEMP"] = str(CACHE / "tmp")
os.environ["XDG_CACHE_HOME"] = str(CACHE)

# ---------------- sys.path + import config (AFTER env) ----------------
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from log_utils import setup_timestamped_print, log_warn, log_error, set_log_file

setup_timestamped_print()

from config import ServerConfig, load_config
from vosk_models import ensure_vosk_model
PYWIN32_HINT = "Install pywin32"
INI_FILENAMES = ("DVCRuntime.ini", "Dragonborn Voice Control.ini")

# ---------------- logging to file (and console) ----------------
class Tee:
    def __init__(self, f):
        self.f = f
    def write(self, s):
        try:
            self.f.write(s); self.f.flush()
        except Exception:
            pass
        try:
            sys.__stdout__.write(s); sys.__stdout__.flush()
        except Exception:
            pass
    def flush(self):
        try:
            self.f.flush()
        except Exception:
            pass
    def isatty(self) -> bool:
        try:
            return bool(getattr(sys.__stdout__, "isatty", lambda: False)())
        except Exception:
            return False

    @property
    def encoding(self) -> str:
        return getattr(sys.__stdout__, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(sys.__stdout__, "errors", "replace")

def _init_log():
    try:
        try:
            if LOG_PATH_01.exists():
                LOG_PATH_01.unlink()
            if LOG_PATH_00.exists():
                LOG_PATH_00.replace(LOG_PATH_01)
        except Exception:
            pass

        f = open(LOG_PATH_00, "w", encoding="utf-8", buffering=1)
        set_log_file(f)
        sys.stdout = Tee(f)
        sys.stderr = Tee(f)
        _print_server_header()
    except Exception:
        pass


def _print_server_header() -> None:
    console = Console(file=sys.__stdout__, force_terminal=True)
    console.print(Rule(style="dim"))
    console.print(Align.center("[bold green]Dragonborn Voice Control[/]"))
    console.print(Rule(style="dim"))
    console.print()

_init_log()

def _ini_from_argv(argv: list[str]) -> Path | None:
    for i in range(len(argv) - 1):
        if argv[i].lower() != "--ini":
            continue
        p = Path(argv[i + 1]).expanduser()
        if p.exists():
            return p
        break
    return None


def _iter_default_ini_candidates():
    yield from (MOD_DIR / "SKSE" / "Plugins" / name for name in INI_FILENAMES)
    yield from (MOD_DIR / name for name in INI_FILENAMES)


def _iter_fallback_ini_candidates(max_levels: int = 6):
    for base in list(RUNTIME_DIR.parents)[:max_levels]:
        yield from (base / "Data" / "SKSE" / "Plugins" / name for name in INI_FILENAMES)
        yield from (base / name for name in INI_FILENAMES)


def _find_ini() -> Path | None:
    ini_arg = _ini_from_argv(sys.argv)
    if ini_arg is not None:
        return ini_arg

    for cand in _iter_default_ini_candidates():
        if cand.exists():
            return cand

    for cand in _iter_fallback_ini_candidates():
        if cand.exists():
            return cand
    return None


def _resolve_asr_settings(cfg):
    asr_engine = (
        _argv_get("asr")
        or os.environ.get("DVC_ASR_ENGINE")
        or cfg.asr_engine
    ).strip().lower()
    arg_lang = _argv_get("lang")
    env_lang = os.environ.get("DVC_ASR_LANG")
    asr_lang = (arg_lang or env_lang or (cfg.asr_lang if cfg.asr_lang_specified else "")).strip().lower()
    vosk_model_name = (
        os.environ.get("DVC_VOSK_MODEL")
        or cfg.vosk_model
    ).strip()
    shouts_backend = (
        os.environ.get("DVC_SHOUTS_BACKEND")
        or cfg.shouts_backend
    ).strip().lower()
    shouts_lang = (
        os.environ.get("DVC_SHOUTS_LANG")
        or cfg.shouts_language
    ).strip().lower()
    shouts_vosk_model_name = (
        os.environ.get("DVC_SHOUTS_VOSK_MODEL")
        or cfg.shouts_vosk_model
    ).strip()
    return {
        "asr_engine": asr_engine,
        "asr_lang": asr_lang,
        "asr_lang_specified": bool(arg_lang or env_lang or cfg.asr_lang_specified),
        "vosk_model_name": vosk_model_name,
        "shouts_backend": shouts_backend,
        "shouts_lang": shouts_lang,
        "shouts_vosk_model_name": shouts_vosk_model_name,
    }


def _resolve_backend_settings(cfg):
    backend = (
        _argv_get("backend")
        or os.environ.get("DVC_BACKEND")
        or cfg.backend
    ).strip().lower()
    arg_cuda = _argv_get("cuda")
    env_cuda = os.environ.get("DVC_CUDA")
    cuda = (arg_cuda or env_cuda or (cfg.cuda if cfg.cuda_specified else "")).strip().lower()
    return backend, cuda, bool(arg_cuda or env_cuda or cfg.cuda_specified)


def _ensure_vosk_models_for_settings(asr: dict) -> str:
    vosk_model_path = ""
    if asr["asr_engine"] == "vosk":
        try:
            mp = ensure_vosk_model(asr["vosk_model_name"], CACHE / "vosk")
            vosk_model_path = str(mp)
        except Exception as e:
            log_error(f"[VOSK][FATAL] {e}")
            raise
    return vosk_model_path


def _export_runtime_env(cfg, asr: dict, backend_eff: str, cuda: str, vosk_model_path: str, *, cuda_specified: bool) -> None:
    os.environ["DVC_BACKEND"] = backend_eff
    if cuda_specified:
        os.environ["DVC_CUDA"] = cuda
    else:
        os.environ.pop("DVC_CUDA", None)

    os.environ["DVC_ASR_ENGINE"] = asr["asr_engine"]
    if asr.get("asr_lang_specified"):
        os.environ["DVC_ASR_LANG"] = asr["asr_lang"]
    else:
        os.environ.pop("DVC_ASR_LANG", None)
    os.environ["DVC_VOSK_MODEL"] = asr["vosk_model_name"]
    if vosk_model_path:
        os.environ["DVC_VOSK_MODEL_PATH"] = vosk_model_path

    os.environ["DVC_WHISPER_MODEL"] = str(cfg.whisper_model)
    if asr.get("asr_lang_specified"):
        os.environ["DVC_WHISPER_LANG"] = str(asr["asr_lang"])
    else:
        os.environ.pop("DVC_WHISPER_LANG", None)
    os.environ["DVC_WHISPER_BEAM"] = str(cfg.whisper_beam)
    os.environ["DVC_WHISPER_CMD_BEAM"] = str(cfg.whisper_command_beam)
    os.environ["DVC_WHISPER_CMD_BEST_OF"] = str(cfg.whisper_command_best_of)
    os.environ["DVC_WHISPER_CMD_TEMPERATURE"] = str(cfg.whisper_command_temperature)
    os.environ["DVC_WHISPER_CMD_LOGPROB"] = str(cfg.whisper_command_log_prob_threshold)
    os.environ["DVC_WHISPER_CMD_NOSPEECH"] = str(cfg.whisper_command_no_speech_threshold)
    os.environ["DVC_WHISPER_CMD_COMPRESSION"] = str(cfg.whisper_command_compression_ratio_threshold)
    os.environ["DVC_WHISPER_CMD_REPETITION"] = str(cfg.whisper_command_repetition_penalty)
    os.environ["DVC_WHISPER_CMD_NO_REPEAT_NGRAM"] = str(cfg.whisper_command_no_repeat_ngram_size)
    os.environ["DVC_WHISPER_CMD_MAX_NEW_TOKENS"] = str(cfg.whisper_command_max_new_tokens)
    os.environ["DVC_WHISPER_CMD_MAX_WORDS"] = str(cfg.whisper_command_max_words)
    os.environ["DVC_WHISPER_CMD_WORD_SLACK"] = str(cfg.whisper_command_word_slack)

    os.environ["DVC_MIN_SCORE"] = str(cfg.min_score)
    os.environ["DVC_MIN_DIFF"] = str(cfg.min_diff)

    os.environ["DVC_MODE"] = str(cfg.mode)
    os.environ["DVC_SetMic"] = str(cfg.SetMic)
    os.environ["DVC_PTT_KEY"] = str(cfg.ptt_key)
    os.environ["DVC_PTT_SEC"] = str(cfg.ptt_sec)

    os.environ["DVC_VAD_START_MS"] = str(cfg.vad_start_ms)
    os.environ["DVC_VAD_END_SIL_MS"] = str(cfg.vad_end_sil_ms)
    os.environ["DVC_VAD_MAX_UTT"] = str(cfg.vad_max_utt)
    os.environ["DVC_VAD_MIN_UTT"] = str(cfg.vad_min_utt)
    os.environ["DVC_VAD_MAX_WAIT"] = str(cfg.vad_max_wait)
    os.environ["DVC_VAD_THR"] = str(cfg.vad_thr)
    os.environ["DVC_VAD_PREROLL_MS"] = str(cfg.vad_preroll_ms)

    os.environ.setdefault("DVC_DEBUG", "0")
    os.environ.setdefault("DVC_SAVE_WAV", "0")
    os.environ["DVC_INMEM_AUDIO"] = "1" if bool(cfg.inmem_audio) else "0"

    open_phrases = cfg.open_phrases
    os.environ["DVC_OPEN_PHRASES"] = str(open_phrases)
    os.environ["DVC_OPEN_SCORE_THR"] = str(cfg.open_score_thr)
    os.environ["DVC_OPEN_MAX_REC_SEC"] = str(cfg.open_max_rec_sec)
    os.environ["DVC_OPEN_VAD_END_SIL_MS"] = str(cfg.open_vad_end_sil_ms)

    os.environ["DVC_CLOSE_PHRASES"] = str(cfg.close_phrases)
    os.environ["DVC_CLOSE_SCORE_THR"] = str(cfg.close_score_thr)

    os.environ["DVC_OPEN_ENABLE_OPEN"] = "1" if bool(cfg.open_enable_open) else "0"
    os.environ["DVC_CLOSE_ENABLE_VOICE"] = "1" if bool(cfg.close_enable_voice) else "0"

    os.environ["DVC_SHOUTS_ENABLE"] = "1" if bool(cfg.shouts_enable) else "0"
    os.environ["DVC_SHOUTS_BACKEND"] = str(asr["shouts_backend"])
    os.environ["DVC_SHOUTS_LANG"] = str(asr["shouts_lang"])
    os.environ["DVC_SHOUTS_VOSK_MODEL"] = str(asr["shouts_vosk_model_name"])
    # Shouts Vosk model path is resolved lazily at runtime on CFG|SHOUTS|1.
    os.environ.pop("DVC_SHOUTS_VOSK_MODEL_PATH", None)


def _run() -> None:
    if _handle_audio_device_check():
        return

    if not IS_FROZEN:
        if not PY_EXE.exists():
            log_error(f"[FATAL] portable python missing: {PY_EXE}")
            print("Press Enter…")
            input()
            sys.exit(1)

        if _relaunch_with_portable_python():
            sys.exit(0)

    ini = _find_ini()
    cfg = load_config(ini) if ini else ServerConfig()

    asr = _resolve_asr_settings(cfg)

    # reflect overrides in cfg for logging
    try:
        cfg.asr_engine = asr["asr_engine"]
        if asr.get("asr_lang_specified"):
            cfg.asr_lang = asr["asr_lang"]
            cfg.asr_lang_specified = True
        cfg.vosk_model = asr["vosk_model_name"]
    except Exception:
        pass

    backend_req, cuda, cuda_specified = _resolve_backend_settings(cfg)
    backend_eff = backend_req

    _check_runtime_deps(asr["asr_engine"], asr["shouts_backend"])

    if asr["asr_engine"] == "whisper" and backend_eff in ("gpu", "auto"):
        ok = _gpu_available_for_whisper()
        if not ok:
            if backend_eff == "gpu":
                log_warn("[BOOT][WARN] GPU requested but not available for faster-whisper (ctranslate2). Falling back to CPU.")
            backend_eff = "cpu"

    vosk_model_path = ""
    #if asr["asr_engine"] == "vosk":
       #print("[VOSK] model selection deferred until client language is received", flush=True)
    _print_ini_cfg(ini, cfg, backend_req, backend_eff, cuda, cuda_specified)

    _export_runtime_env(cfg, asr, backend_eff, cuda, vosk_model_path, cuda_specified=cuda_specified)

    from pipe_server import main as server_main
    server_main(cfg)


def _handle_audio_device_check() -> bool:
    if not any(a.lower() == "--check-audio-device" for a in sys.argv):
        return False

    try:
        try:
            import sd as dvc_sd
            if hasattr(dvc_sd, "main"):
                dvc_sd.main()
                return True
        except Exception:
            pass

        import sounddevice as sd

        devices = sd.query_devices()
        default_in, _default_out = sd.default.device

        if default_in is not None and default_in >= 0:
            d = sd.query_devices(default_in)
            print(f"\nCurrent default input: [{default_in}] {d['name']}")
        else:
            print("\nCurrent default input: (not set)\n")
        print("Hint: in ini you can set [Mode] SetMic=<index>.\n")
        print("Input devices (recording):")

        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) > 0:
                mark = "  <-- default input" if i == default_in else ""
                hostapi = sd.query_hostapis(d.get("hostapi", 0)).get("name", "?")
                print(
                    f"[{i}] {d.get('name', '?')} | hostapi: {hostapi} | "
                    f"in_ch: {d.get('max_input_channels', 0)} | "
                    f"default_sr: {d.get('default_samplerate', 0)}{mark}"
                )
        return True
    finally:
        try:
            if sys.stdin and getattr(sys.stdin, "isatty", lambda: False)():
                input("\nPress Enter…")
        except Exception:
            pass

def _require_import(module_name: str, hint: str = "") -> None:
    try:
        __import__(module_name)
    except Exception as e:
        extra = f"\n{hint}" if hint else ""
        raise RuntimeError(f"Missing Python dependency: {module_name} ({e}).{extra}")

def _check_runtime_deps(asr_engine: str, shouts_backend: str) -> None:
    # Base deps (needed regardless of ASR)
    _require_import("numpy")
    _require_import("sounddevice")
    _require_import("keyboard")
    _require_import("win32pipe", hint=PYWIN32_HINT)
    _require_import("win32file", hint=PYWIN32_HINT)
    _require_import("pywintypes", hint=PYWIN32_HINT)

    if asr_engine == "whisper":
        _require_import("faster_whisper")
        _require_import("ctranslate2")
        _require_import("av")
        if shouts_backend == "vosk":
            _require_import("vosk")
        return

    if asr_engine == "vosk":
        _require_import("vosk")
        return

    raise RuntimeError(f"Unknown ASR engine: {asr_engine}")

def _ctranslate2_cuda_available() -> bool:
    """GPU support for faster-whisper depends on CUDA availability in ctranslate2; torch is not used as a signal."""
    try:
        import ctranslate2 as ct
        types = ct.get_supported_compute_types("cuda")
        return bool(types)
    except Exception:
        return False

def _gpu_available_for_whisper() -> bool:
    return _ctranslate2_cuda_available()

def _relaunch_with_portable_python():
    if IS_FROZEN:
        return False
    if Path(sys.executable).resolve() != PY_EXE.resolve():
        print(f"[BOOT] relaunching with portable python: {PY_EXE}", flush=True)
        cmd = [str(PY_EXE), str(Path(__file__).resolve()), *sys.argv[1:]]
        subprocess.Popen(cmd, cwd=str(RUNTIME_DIR), env=os.environ.copy())
        return True
    return False

def _argv_get(name: str) -> str | None:
    argv = [a.strip() for a in sys.argv]
    for i in range(len(argv) - 1):
        if argv[i].lower() == f"--{name.lower()}":
            return argv[i + 1]
    return None

def _print_ini_cfg(ini_path: Path | None, cfg, backend_req: str, backend_eff: str, cuda: str, cuda_specified: bool):
    ini_str = str(ini_path) if ini_path else "(none)"
    print(f"[INI] file={ini_str}", flush=True)
    parts: list[str] = []

    parts.append(f"ASR.Engine={cfg.asr_engine}")

    if cfg.asr_engine == "whisper":
        if str(cfg.asr_lang).strip() and bool(getattr(cfg, "asr_lang_specified", False)):
            parts.append(f"Whisper.Language={cfg.asr_lang}")
        parts.append(f"Whisper.Backend={backend_req} (effective={backend_eff})")
        if str(cuda).strip() and cuda_specified:
            parts.append(f"Whisper.Cuda={cuda}")
        if str(cfg.whisper_model).strip():
            parts.append(f"Whisper.Model={cfg.whisper_model}")
        parts.append(f"Whisper.BeamSize={cfg.whisper_beam}")
        parts.append(f"Whisper.InMemAudio={int(bool(cfg.inmem_audio))}")
    elif cfg.asr_engine == "vosk":
        if str(cfg.vosk_model).strip():
            parts.append(f"Vosk.Model={cfg.vosk_model}")

    parts.append(f"Mode.Mode={cfg.mode}")
    if str(cfg.SetMic).strip():
        parts.append(f"Mode.SetMic={cfg.SetMic}")

    if cfg.mode == "vad":
        parts.append(f"VAD.Threshold={cfg.vad_thr}")
        parts.append(f"VAD.StartMs={cfg.vad_start_ms}")
        parts.append(f"VAD.EndSilenceMs={cfg.vad_end_sil_ms}")
        parts.append(f"VAD.PreRollMs={cfg.vad_preroll_ms}")
        parts.append(f"VAD.MaxUttSec={cfg.vad_max_utt}")
        parts.append(f"VAD.MinUttSec={cfg.vad_min_utt}")
        parts.append(f"VAD.MaxWaitSec={cfg.vad_max_wait}")
    elif cfg.mode == "ptt":
        parts.append(f"PTT.Hotkey={cfg.ptt_key}")
        parts.append(f"PTT.Seconds={cfg.ptt_sec}")

    parts.append(f"Matching.MinScore={cfg.min_score}")
    parts.append(f"Matching.MinDiff={cfg.min_diff}")

    print(f"[INI] {' | '.join(parts)}", flush=True)

if __name__ == "__main__":
    try:
        _run()

    except Exception:
        log_error(traceback.format_exc())
        log_error("[Server crashed] Press Enter to exit...")
        try:
            noninteractive = os.environ.get("DVC_NONINTERACTIVE", "0").strip() in ("1", "true", "yes")
            if (not noninteractive) and sys.stdin and getattr(sys.stdin, "isatty", lambda: False)():
                input()
        except Exception:
            pass
        sys.exit(1)