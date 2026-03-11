from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

import matching
from config import ServerConfig
from vosk_models import ensure_vosk_model
from log_utils import setup_timestamped_print, log_warn, log_error

setup_timestamped_print()


SR = 16000
WAV_DEBUG_DIR_REL = "caches/vad_caps"


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)).strip())
    except Exception:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)).strip())
    except Exception:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "1" if default else "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _pick_backend_device_compute(default_backend: str) -> tuple[str, str]:
    backend = _env_str("DVC_BACKEND", str(default_backend)).lower()
    if backend in ("gpu", "cuda"):
        return "cuda", "float16"
    return "cpu", "int8"


def _wav_write_int16(path: Path, data: np.ndarray, sr: int = SR) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(data.tobytes())


def _maybe_add_cuda_dll_dirs() -> None:
    # Optional: helps ctranslate2 locate CUDA DLLs inside portable site-packages.
    try:
        candidates: list[Path] = []
        for p in sys.path:
            if not p:
                continue
            sp = str(p)
            if "site-packages" in sp:
                candidates.append(Path(p))

        dll_dirs: list[Path] = []
        for base in candidates:
            dll_dirs += [
                base / "nvidia" / "cublas" / "bin",
                base / "nvidia" / "cudnn" / "bin",
                base / "nvidia" / "cuda_runtime" / "bin",
            ]

        for d in dll_dirs:
            if d.exists():
                try:
                    os.add_dll_directory(str(d))
                except Exception:
                    pass
    except Exception:
        pass


class Recognizer:
    def __init__(self, cfg=None):
        self.cfg = cfg if cfg is not None else ServerConfig()
        self._whisper_model = None
        self._vosk_model = None
        self._vosk_shouts_model = None
        self._vosk_open_grammar_json = None
        self._vosk_close_grammar_json = None
        self._vosk_dialog_grammar_json = None
        self._vosk_dialog_rec = None
        self._shout_recognizer = None  # Lazy-loaded ShoutRecognizer
        self._allowed_shout_formids: set[str] | None = None
        self._allowed_shout_entries: list[tuple[str, str, str, str]] | None = None  # (plugin, formid, name, editorID)
        self._allowed_power_entries: list[tuple[str, str]] | None = None
        self._allowed_weapon_entries: list[tuple[str, str]] | None = None
        self._allowed_spell_entries: list[tuple[str, str]] | None = None
        self._allowed_potion_entries: list[tuple[str, str]] | None = None
        self._power_phrase_to_formid: dict[str, str] | None = None
        self._vosk_power_grammar_json: str | None = None
        self._vosk_power_rec = None

        # Weapon/Spell/Potion item recognition
        self._weapon_phrase_to_formid: dict[str, str] | None = None
        self._vosk_weapon_grammar_json: str | None = None
        self._vosk_weapon_rec = None

        self._spell_phrase_to_formid: dict[str, str] | None = None
        self._vosk_spell_grammar_json: str | None = None
        self._vosk_spell_rec = None

        self._potion_phrase_to_formid: dict[str, str] | None = None
        self._vosk_potion_grammar_json: str | None = None
        self._vosk_potion_rec = None

        cfg_engine = self.cfg.asr_engine
        cfg_lang = self.cfg.asr_lang

        self.asr_engine = _env_str("DVC_ASR_ENGINE", str(cfg_engine)).lower()
        self.asr_lang = _env_str("DVC_ASR_LANG", str(cfg_lang)).lower()
        cfg_shouts_lang = self.cfg.shouts_language
        self.shouts_lang = _env_str("DVC_SHOUTS_LANG", str(cfg_shouts_lang)).lower()
        # Remember whether the configuration explicitly provided shouts model/lang
        # (as opposed to the server applying a language-based fallback).
        self._cfg_shouts_vosk_specified = bool(str(getattr(self.cfg, "shouts_vosk_model", "")).strip())
        self._cfg_shouts_lang_specified = bool(str(getattr(self.cfg, "shouts_language", "")).strip())

        cfg_model = self.cfg.whisper_model
        cfg_beam = self.cfg.whisper_beam
        self.model_size = _env_str("DVC_WHISPER_MODEL", str(cfg_model))
        self.whisper_beam = _env_int("DVC_WHISPER_BEAM", int(cfg_beam))
        self.whisper_command_beam = _env_int("DVC_WHISPER_CMD_BEAM", int(self.cfg.whisper_command_beam))
        self.whisper_command_best_of = _env_int("DVC_WHISPER_CMD_BEST_OF", int(self.cfg.whisper_command_best_of))
        self.whisper_command_temperature = _env_float("DVC_WHISPER_CMD_TEMPERATURE", float(self.cfg.whisper_command_temperature))
        self.whisper_command_log_prob_threshold = _env_float("DVC_WHISPER_CMD_LOGPROB", float(self.cfg.whisper_command_log_prob_threshold))
        self.whisper_command_no_speech_threshold = _env_float("DVC_WHISPER_CMD_NOSPEECH", float(self.cfg.whisper_command_no_speech_threshold))
        self.whisper_command_compression_ratio_threshold = _env_float("DVC_WHISPER_CMD_COMPRESSION", float(self.cfg.whisper_command_compression_ratio_threshold))
        self.whisper_command_repetition_penalty = _env_float("DVC_WHISPER_CMD_REPETITION", float(self.cfg.whisper_command_repetition_penalty))
        self.whisper_command_no_repeat_ngram_size = _env_int("DVC_WHISPER_CMD_NO_REPEAT_NGRAM", int(self.cfg.whisper_command_no_repeat_ngram_size))
        self.whisper_command_max_new_tokens = _env_int("DVC_WHISPER_CMD_MAX_NEW_TOKENS", int(self.cfg.whisper_command_max_new_tokens))
        self.whisper_command_max_words = _env_int("DVC_WHISPER_CMD_MAX_WORDS", int(self.cfg.whisper_command_max_words))
        self.whisper_command_word_slack = _env_int("DVC_WHISPER_CMD_WORD_SLACK", int(self.cfg.whisper_command_word_slack))

        cfg_vosk = self.cfg.vosk_model
        self.vosk_model_path = _env_str("DVC_VOSK_MODEL_PATH", "")
        self.vosk_model_name = _env_str("DVC_VOSK_MODEL", str(cfg_vosk))
        cfg_shouts_vosk = self.cfg.shouts_vosk_model
        self.shouts_vosk_model_path = _env_str("DVC_SHOUTS_VOSK_MODEL_PATH", "")
        self.shouts_vosk_model_name = _env_str("DVC_SHOUTS_VOSK_MODEL", str(cfg_shouts_vosk))

        cfg_inmem = bool(self.cfg.inmem_audio)
        self.debug_enabled = _env_bool("DVC_DEBUG", False)
        self.save_wav_enabled = _env_bool("DVC_SAVE_WAV", False)
        # WAV capture for debug is handled in pipe_server where we know context
        # (open/dialogue/close/shout) and can name files accordingly.
        self.save_wav_debug = False
        self.wav_dir_rel = WAV_DEBUG_DIR_REL
        self.use_inmem_audio = _env_bool("DVC_INMEM_AUDIO", cfg_inmem)

        cache_root_env = os.environ.get("DVC_CACHE_DIR", "").strip()
        if cache_root_env:
            cache_root = Path(cache_root_env).expanduser().resolve()
        elif bool(getattr(sys, "frozen", False)):
            cache_root = Path(sys.executable).resolve().parent
        else:
            cache_root = Path(__file__).resolve().parent
        self.runtime_dir = cache_root
        self.wav_debug_dir = (self.runtime_dir / self.wav_dir_rel).resolve()
        self.wav_debug_dir.mkdir(parents=True, exist_ok=True)

        self.device, self.compute_type = _pick_backend_device_compute(self.cfg.backend)

    def set_debug_enabled(self, enabled: bool) -> None:
        self.debug_enabled = bool(enabled)
        # Keep ASR-internal WAV dump disabled (server-side contextual dump is used).
        self.save_wav_debug = False
        os.environ["DVC_DEBUG"] = "1" if enabled else "0"

    def set_save_wav_enabled(self, enabled: bool) -> None:
        self.save_wav_enabled = bool(enabled)
        os.environ["DVC_SAVE_WAV"] = "1" if enabled else "0"

    def _phrases_list(self, kind: str) -> list[str]:
        # kind: "open" or "close"
        if kind == "open":
            default = self.cfg.open_phrases
            s = _env_str(
                "DVC_OPEN_PHRASES",
                _env_str("DVC_OPEN_PHRASES", str(default)),
            )
        elif kind == "close":
            default = self.cfg.close_phrases
            s = _env_str("DVC_CLOSE_PHRASES", str(default))
        else:
            s = ""

        phrases: list[str] = []
        for p in str(s).split(","):
            n = matching.normalize(p)
            if n:
                phrases.append(n)
        return phrases

    def _vosk_grammar_json(self, kind: str) -> str:
        phrases = self._phrases_list(kind)
        # Vosk expects JSON array of strings
        j = json.dumps(phrases, ensure_ascii=False)
        if kind == "open":
            if self._vosk_open_grammar_json != j:
                self._vosk_open_grammar_json = j
            return self._vosk_open_grammar_json
        if kind == "close":
            if self._vosk_close_grammar_json != j:
                self._vosk_close_grammar_json = j
            return self._vosk_close_grammar_json
        return j

    def warmup(self) -> None:
        # Optional: allow main/pipe_server to trigger model loading.
        if self.asr_engine == "whisper":
            self._ensure_whisper()
        elif self.asr_engine == "vosk":
            self._ensure_vosk()

    def _ensure_whisper(self):
        if self._whisper_model is not None:
            return self._whisper_model
        _maybe_add_cuda_dll_dirs()
        try:
            from faster_whisper import WhisperModel
        except Exception as e:
            raise RuntimeError(f"faster-whisper is not installed: {e}")

        self._whisper_model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        return self._whisper_model

    def _ensure_vosk(self):
        if self._vosk_model is not None:
            return self._vosk_model

        if not self.vosk_model_path:
            raise RuntimeError(
                "Vosk selected but DVC_VOSK_MODEL_PATH is empty. "
                "Check DVCRuntime.ini [ASR]/[Vosk] and server boot logs."
            )

        try:
            from vosk import Model as VoskModel
        except Exception as e:
            raise RuntimeError(f"Vosk is not installed: {e}")

        self._vosk_model = VoskModel(self.vosk_model_path)
        return self._vosk_model

    def _ensure_vosk_shouts(self):
        if self._vosk_shouts_model is not None:
            return self._vosk_shouts_model

        # If the main ASR already runs on Vosk, reuse the same loaded model
        # only when the requested shouts model matches the main model.
        if self.asr_engine == "vosk":
            main_name = (self.vosk_model_name or "").strip()
            shouts_name = (self.shouts_vosk_model_name or "").strip()
            if (not shouts_name) or (shouts_name == main_name):
                self._vosk_shouts_model = self._ensure_vosk()
                return self._vosk_shouts_model

        # Resolve shouts model lazily (supports runtime CFG|SHOUTS|1 without restart).
        model_name = (self.shouts_vosk_model_name or self.vosk_model_name).strip()
        if not model_name:
            raise RuntimeError("Shouts Vosk model is empty (DVC_SHOUTS_VOSK_MODEL)")

        if not self.shouts_vosk_model_path:
            default_selected = (not self._cfg_shouts_vosk_specified) and (not self._cfg_shouts_lang_specified)
            reason = "default" if default_selected else "configured"
            print(
                f"[SHOUT] Loading Vosk model for shouts ({model_name}, lang={self.shouts_lang}, reason={reason})",
                flush=True,
            )
            cache_dir = (self.runtime_dir / "caches" / "vosk").resolve()
            model_dir = ensure_vosk_model(model_name, cache_dir)
            self.shouts_vosk_model_path = str(model_dir)
            os.environ["DVC_SHOUTS_VOSK_MODEL_PATH"] = self.shouts_vosk_model_path

        try:
            from vosk import Model as VoskModel
        except Exception as e:
            raise RuntimeError(f"Vosk is not installed: {e}")

        self._vosk_shouts_model = VoskModel(self.shouts_vosk_model_path)
        return self._vosk_shouts_model

    def _ensure_vosk_items(self):
        # Items/powers should use the main model when available.
        if self.asr_engine == "vosk":
            return self._ensure_vosk()
        return self._ensure_vosk_shouts()

    def _whisper_commands_enabled(self) -> bool:
        return bool(self.asr_engine == "whisper")

    def uses_whisper_for_commands(self) -> bool:
        return self._whisper_commands_enabled()

    def _build_whisper_command_mapping(
        self,
        entries: list[tuple[str, str]] | None,
    ) -> tuple[dict[str, str] | None, list[str]]:
        """Build whisper mapping (normalized phrase -> formid) + raw phrases list."""
        if not entries:
            return None, []
        mapping: dict[str, str] = {}
        phrases: list[str] = []
        for formid, name in entries:
            formid_hex = self._normalize_shout_formid(formid)
            phrase = matching.normalize(str(name or ""))
            if not formid_hex or not phrase:
                continue
            if phrase in mapping:
                continue
            mapping[phrase] = formid_hex
            phrases.append(str(name or "").strip())
        if not phrases:
            return None, []
        return mapping, phrases

    def _configured_phrase_strings(self, kind: str) -> list[str]:
        if kind == "open":
            source = self.cfg.open_phrases or ""
            env_key = "DVC_OPEN_PHRASES"
        elif kind == "close":
            source = self.cfg.close_phrases or ""
            env_key = "DVC_CLOSE_PHRASES"
        else:
            return []

        phrases: list[str] = []
        seen: set[str] = set()
        for raw in _env_str(env_key, str(source)).split(","):
            phrase = str(raw or "").strip()
            norm = matching.normalize(phrase)
            if not phrase or not norm or norm in seen:
                continue
            seen.add(norm)
            phrases.append(phrase)
        return phrases

    def _unique_phrases(self, phrases: list[str] | None) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw in phrases or []:
            phrase = str(raw or "").strip()
            norm = matching.normalize(phrase)
            if not phrase or not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(phrase)
        return out

    def _command_candidate_phrases(self, enabled_categories: list[str]) -> list[str]:
        phrases: list[str] = []
        for kind in enabled_categories:
            if kind == "power":
                phrases.extend(self._entries_to_phrases(self._allowed_power_entries))
            elif kind == "weapon":
                phrases.extend(self._entries_to_phrases(self._allowed_weapon_entries))
            elif kind == "spell":
                phrases.extend(self._entries_to_phrases(self._allowed_spell_entries))
            elif kind == "potion":
                phrases.extend(self._entries_to_phrases(self._allowed_potion_entries))
        return self._unique_phrases(phrases)

    def _command_hotwords_text(self, candidate_phrases: list[str] | None) -> str | None:
        hotwords: list[str] = []
        total_chars = 0
        for phrase in self._unique_phrases(candidate_phrases):
            add_len = len(phrase) + (2 if hotwords else 0)
            if len(hotwords) >= 24 or total_chars + add_len > 320:
                break
            hotwords.append(phrase)
            total_chars += add_len
        if not hotwords:
            return None
        return ", ".join(hotwords)

    def _command_word_budget(self, candidate_phrases: list[str] | None) -> int:
        longest = 0
        for phrase in self._unique_phrases(candidate_phrases):
            longest = max(longest, len(matching.tokens(phrase)))
        fallback = max(1, int(self.whisper_command_max_words))
        slack = max(0, int(self.whisper_command_word_slack))
        return max(fallback, longest + slack)

    def _command_max_new_tokens_budget(self, candidate_phrases: list[str] | None) -> int:
        word_budget = self._command_word_budget(candidate_phrases)
        derived = max(8, (word_budget * 2) + 2)
        return min(max(1, int(self.whisper_command_max_new_tokens)), derived)

    def _recognized_text_from_segments(self, segments) -> tuple[str, list[object]]:
        seg_list = list(segments)
        text = "".join(str(getattr(seg, "text", "") or "") for seg in seg_list).strip()
        return text, seg_list

    def _whisper_metric(self, segments: list[object], attr: str, reducer=max, default=0.0) -> float:
        values: list[float] = []
        for seg in segments:
            value = getattr(seg, attr, None)
            if value is None:
                continue
            try:
                values.append(float(value))
            except Exception:
                continue
        if not values:
            return float(default)
        return float(reducer(values))

    def _transcribe_whisper_segments(self, source, **kwargs) -> tuple[str, list[object]]:
        model = self._ensure_whisper()
        segments, _ = model.transcribe(source, language=self.asr_lang, **kwargs)
        return self._recognized_text_from_segments(segments)

    def _filter_whisper_command_text(
        self,
        text: str,
        segments: list[object],
        candidate_phrases: list[str] | None,
    ) -> tuple[str, dict]:
        norm = matching.normalize(text)
        token_list = matching.tokens(text)
        candidate_norms = {matching.normalize(phrase) for phrase in self._unique_phrases(candidate_phrases)}
        avg_logprob = self._whisper_metric(segments, "avg_logprob", reducer=lambda values: sum(values) / len(values), default=0.0)
        compression_ratio = self._whisper_metric(segments, "compression_ratio", reducer=max, default=0.0)
        no_speech_prob = self._whisper_metric(segments, "no_speech_prob", reducer=max, default=0.0)
        stats = {
            "raw_text": text,
            "avg_logprob": avg_logprob,
            "compression_ratio": compression_ratio,
            "no_speech_prob": no_speech_prob,
            "word_count": len(token_list),
            "word_budget": self._command_word_budget(candidate_phrases),
            "reason": "ok",
        }

        if not norm:
            stats["reason"] = "empty"
            return "", stats

        if norm in candidate_norms:
            return text, stats

        if len(token_list) > stats["word_budget"]:
            stats["reason"] = "hallucination_filter"
            return "", stats

        if compression_ratio > float(self.whisper_command_compression_ratio_threshold):
            stats["reason"] = "hallucination_filter"
            return "", stats

        if avg_logprob < float(self.whisper_command_log_prob_threshold):
            stats["reason"] = "low_confidence"
            return "", stats

        if no_speech_prob > float(self.whisper_command_no_speech_threshold) and avg_logprob < 0.0:
            stats["reason"] = "low_confidence"
            return "", stats

        return text, stats

    def _transcribe_whisper_command(self, pcm16: np.ndarray, *, candidate_phrases: list[str] | None = None):
        t0 = time.perf_counter()
        hotwords = self._command_hotwords_text(candidate_phrases)
        kwargs = {
            "beam_size": max(1, int(self.whisper_command_beam)),
            "best_of": max(1, int(self.whisper_command_best_of)),
            "temperature": float(self.whisper_command_temperature),
            "condition_on_previous_text": False,
            "without_timestamps": True,
            "log_prob_threshold": float(self.whisper_command_log_prob_threshold),
            "no_speech_threshold": float(self.whisper_command_no_speech_threshold),
            "compression_ratio_threshold": float(self.whisper_command_compression_ratio_threshold),
            "repetition_penalty": float(self.whisper_command_repetition_penalty),
            "no_repeat_ngram_size": max(0, int(self.whisper_command_no_repeat_ngram_size)),
            "max_new_tokens": self._command_max_new_tokens_budget(candidate_phrases),
        }
        if hotwords:
            kwargs["hotwords"] = hotwords

        if self.use_inmem_audio:
            t_wh0 = time.perf_counter()
            audio = (pcm16.astype(np.float32) / 32768.0)
            text, segments = self._transcribe_whisper_segments(audio, **kwargs)
            filtered_text, stats = self._filter_whisper_command_text(text, segments, candidate_phrases)
            t_wh1 = time.perf_counter()
            t_asr = (t_wh1 - t_wh0)
            stats.update({
                "text": filtered_text,
                "t_wav": 0.0,
                "t_asr": t_asr,
                "t_whisper": t_asr,
                "t_total_io": (t_wh1 - t0),
                "wav_path": None,
            })
            return filtered_text, stats

        wav_path: Path | None = None
        t_wav0 = time.perf_counter()
        try:
            if self.save_wav_debug:
                wav_path = self.wav_debug_dir / (
                    time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time()*1000)%1000:03d}.wav"
                )
                _wav_write_int16(wav_path, pcm16, sr=SR)
            else:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
                tmp.close()
                wav_path = Path(tmp.name)
                _wav_write_int16(wav_path, pcm16, sr=SR)

            t_wav1 = time.perf_counter()

            t_wh0 = time.perf_counter()
            text, segments = self._transcribe_whisper_segments(str(wav_path), **kwargs)
            filtered_text, stats = self._filter_whisper_command_text(text, segments, candidate_phrases)
            t_wh1 = time.perf_counter()

            t_asr = (t_wh1 - t_wh0)
            stats.update({
                "text": filtered_text,
                "t_wav": (t_wav1 - t_wav0),
                "t_asr": t_asr,
                "t_whisper": t_asr,
                "t_total_io": (t_wh1 - t0),
                "wav_path": str(wav_path) if self.save_wav_debug else None,
            })
            return filtered_text, stats
        finally:
            if wav_path and (not self.save_wav_debug):
                try:
                    wav_path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _transcribe_whisper(self, pcm16: np.ndarray):
        t0 = time.perf_counter()

        if self.use_inmem_audio:
            t_wh0 = time.perf_counter()
            audio = (pcm16.astype(np.float32) / 32768.0)
            text, _segments = self._transcribe_whisper_segments(audio, beam_size=self.whisper_beam)
            t_wh1 = time.perf_counter()
            t_asr = (t_wh1 - t_wh0)
            return text, {"t_wav": 0.0, "t_asr": t_asr, "t_whisper": t_asr, "t_total_io": (t_wh1 - t0), "wav_path": None}

        wav_path: Path | None = None
        t_wav0 = time.perf_counter()
        try:
            if self.save_wav_debug:
                wav_path = self.wav_debug_dir / (
                    time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time()*1000)%1000:03d}.wav"
                )
                _wav_write_int16(wav_path, pcm16, sr=SR)
            else:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
                tmp.close()
                wav_path = Path(tmp.name)
                _wav_write_int16(wav_path, pcm16, sr=SR)

            t_wav1 = time.perf_counter()

            t_wh0 = time.perf_counter()
            text, _segments = self._transcribe_whisper_segments(str(wav_path), beam_size=self.whisper_beam)
            t_wh1 = time.perf_counter()

            t_asr = (t_wh1 - t_wh0)
            return text, {
                "t_wav": (t_wav1 - t_wav0),
                "t_asr": t_asr,
                "t_whisper": t_asr,
                "t_total_io": (t_wh1 - t0),
                "wav_path": str(wav_path) if self.save_wav_debug else None,
            }
        finally:
            if wav_path and (not self.save_wav_debug):
                try:
                    wav_path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _transcribe_vosk(self, pcm16: np.ndarray):
        model = self._ensure_vosk()
        try:
            from vosk import KaldiRecognizer
        except Exception as e:
            raise RuntimeError(f"Vosk is not installed: {e}")

        t0 = time.perf_counter()

        wav_path: Path | None = None
        t_wav = 0.0
        if self.save_wav_debug:
            t_w0 = time.perf_counter()
            wav_path = self.wav_debug_dir / (
                time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time()*1000)%1000:03d}.wav"
            )
            _wav_write_int16(wav_path, pcm16, sr=SR)
            t_w1 = time.perf_counter()
            t_wav = (t_w1 - t_w0)

        t_asr0 = time.perf_counter()
        rec = KaldiRecognizer(model, SR)
        rec.SetWords(False)
        rec.AcceptWaveform(pcm16.tobytes())
        res = rec.FinalResult()
        try:
            data = json.loads(res)
            text = str(data.get("text", "")).strip()
        except Exception:
            text = ""
        t_asr1 = time.perf_counter()

        return text, {
            "t_wav": t_wav,
            "t_asr": (t_asr1 - t_asr0),
            "t_total_io": (t_asr1 - t0),
            "wav_path": str(wav_path) if wav_path else None,
        }

    def _transcribe_vosk_grammar(self, pcm16: np.ndarray, grammar_json: str):
        model = self._ensure_vosk()
        try:
            from vosk import KaldiRecognizer
        except Exception as e:
            raise RuntimeError(f"Vosk is not installed: {e}")

        t0 = time.perf_counter()
        t_asr0 = time.perf_counter()

        if not grammar_json or grammar_json == "[]":
            return "", {"t_asr": 0.0, "t_total_io": (time.perf_counter() - t0)}

        rec = KaldiRecognizer(model, SR, grammar_json)
        rec.SetWords(False)
        rec.AcceptWaveform(pcm16.tobytes())
        res = rec.FinalResult()
        try:
            data = json.loads(res)
            text = str(data.get("text", "")).strip()
        except Exception:
            text = ""

        t_asr1 = time.perf_counter()
        return text, {"t_asr": (t_asr1 - t_asr0), "t_total_io": (t_asr1 - t0), "grammar": True}

    def _transcribe_vosk_with_recognizer(self, rec, pcm16: np.ndarray):
        t0 = time.perf_counter()
        t_asr0 = time.perf_counter()

        if hasattr(rec, "Reset"):
            try:
                rec.Reset()
            except Exception:
                pass

        rec.SetWords(False)
        rec.AcceptWaveform(pcm16.tobytes())
        res = rec.FinalResult()
        try:
            data = json.loads(res)
            text = str(data.get("text", "")).strip()
        except Exception:
            text = ""

        t_asr1 = time.perf_counter()
        return text, {"t_asr": (t_asr1 - t_asr0), "t_total_io": (t_asr1 - t0), "grammar": True}

    def transcribe_dialogue(self, pcm16: np.ndarray):
        if self.asr_engine == "vosk":
            if self._vosk_dialog_rec is not None:
                return self._transcribe_vosk_with_recognizer(self._vosk_dialog_rec, pcm16)
            return self._transcribe_vosk(pcm16)
        if self.asr_engine == "whisper":
            return self._transcribe_whisper(pcm16)
        raise ValueError(f"Unknown ASR engine: {self.asr_engine}")

    def transcribe_dialogue_grammar(self, pcm16: np.ndarray, grammar_json: str):
        if self.asr_engine == "vosk":
            return self._transcribe_vosk_grammar(pcm16, grammar_json)
        return self.transcribe_dialogue(pcm16)

    def transcribe_dialogue_free(self, pcm16: np.ndarray):
        if self.asr_engine == "vosk":
            return self._transcribe_vosk(pcm16)
        return self.transcribe_dialogue(pcm16)

    def transcribe_open(self, pcm16: np.ndarray):
        if self.asr_engine == "vosk":
            grammar = self._vosk_grammar_json("open")
            return self._transcribe_vosk_grammar(pcm16, grammar)
        return self._transcribe_whisper_command(pcm16, candidate_phrases=self._configured_phrase_strings("open"))

    def transcribe_close(self, pcm16: np.ndarray):
        if self.asr_engine == "vosk":
            grammar = self._vosk_grammar_json("close")
            return self._transcribe_vosk_grammar(pcm16, grammar)
        return self._transcribe_whisper_command(pcm16, candidate_phrases=self._configured_phrase_strings("close"))

    def set_dialog_grammar(self, phrases: list[str] | None) -> None:
        if not phrases:
            self._vosk_dialog_grammar_json = None
            self._vosk_dialog_rec = None
            return

        if self.asr_engine != "vosk":
            self._vosk_dialog_grammar_json = None
            self._vosk_dialog_rec = None
            return

        grammar_json = json.dumps(list(phrases), ensure_ascii=False)
        self._vosk_dialog_grammar_json = grammar_json

        model = self._ensure_vosk()
        try:
            from vosk import KaldiRecognizer
        except Exception as e:
            raise RuntimeError(f"Vosk is not installed: {e}")

        self._vosk_dialog_rec = KaldiRecognizer(model, SR, grammar_json)

    def clear_dialog_grammar(self) -> None:
        self._vosk_dialog_grammar_json = None
        self._vosk_dialog_rec = None

    # =========== SHOUT RECOGNITION (lazy loaded) ===========

    def _shouts_enabled(self) -> bool:
        return _env_bool("DVC_SHOUTS_ENABLE", bool(self.cfg.shouts_enable))

    def _shouts_backend(self) -> str:
        return _env_str("DVC_SHOUTS_BACKEND", str(self.cfg.shouts_backend)).lower()

    def _ensure_shout_recognizer(self):
        if not self._shouts_enabled():
            return None

        if self._shout_recognizer is not None:
            return self._shout_recognizer

        # Import lazily to avoid loading heavy deps when shouts disabled
        from shout_recognition import ShoutRecognizer

        backend = self._shouts_backend()
        if backend != "vosk":
            log_warn(f"[SHOUT][WARN] Unsupported shouts backend '{backend}', falling back to 'vosk'.")
            backend = "vosk"

        self._shout_recognizer = ShoutRecognizer(
            backend=backend,
            lang=self.shouts_lang,
        )

        # Apply current grammar restriction (if any)
        try:
            if self._allowed_shout_entries is not None and hasattr(self._shout_recognizer, "set_allowed_shout_entries"):
                self._shout_recognizer.set_allowed_shout_entries(self._allowed_shout_entries)
            elif self._allowed_shout_formids is not None and hasattr(self._shout_recognizer, "set_allowed_formids"):
                self._shout_recognizer.set_allowed_formids(self._allowed_shout_formids)
        except Exception as e:
            log_warn(f"[SHOUT][WARN] Failed to apply allowed shouts restriction: {e}")

        # Provide Vosk model (reuse the one already loaded for dialogue)
        if self.asr_engine == "vosk" or backend == "vosk":
            vosk_model = self._ensure_vosk_shouts()
            self._shout_recognizer.set_vosk_model(vosk_model)

        return self._shout_recognizer

    def _normalize_shout_formid(self, formid: str) -> str | None:
        s = str(formid or "").strip().upper()
        if not s:
            return None

        raw = s[2:] if s.startswith("0X") else s
        try:
            val = int(raw, 16)
            return f"0x{val:08X}"
        except Exception:
            if s.startswith("0X"):
                return "0x" + s[2:]
            return s

    def _normalize_shout_formids(self, formids: list[str] | set[str]) -> set[str]:
        normalized: set[str] = set()
        for formid in formids:
            value = self._normalize_shout_formid(formid)
            if value:
                normalized.add(value)
        return normalized

    def _build_allowed_shout_formids_from_entries(
        self,
        entries: list[tuple[str, str, str, str]],
    ) -> set[str]:
        norm_keys: list[str] = []
        for plugin, formid, _name, _editor_id in entries:
            plugin_norm = str(plugin or "").strip().lower()
            raw = str(formid or "").strip()
            if not plugin_norm or not raw:
                continue
            raw_hex = raw[2:] if raw.lower().startswith("0x") else raw
            try:
                val = int(raw_hex, 16)
            except Exception:
                continue
            base = val & 0x00FFFFFF
            norm_keys.append(f"{plugin_norm}|0x{base:06x}")
        return self._normalize_shout_formids(norm_keys)

    def _apply_allowed_shout_formids(self) -> None:
        if self._shout_recognizer is not None and hasattr(self._shout_recognizer, "set_allowed_formids"):
            self._shout_recognizer.set_allowed_formids(self._allowed_shout_formids)

    def _apply_allowed_shout_entries(self) -> None:
        if self._shout_recognizer is not None and hasattr(self._shout_recognizer, "set_allowed_shout_entries"):
            self._shout_recognizer.set_allowed_shout_entries(self._allowed_shout_entries)
        else:
            self._apply_allowed_shout_formids()

    def set_allowed_shout_formids(self, formids: list[str] | set[str] | None) -> None:
        if formids is None:
            self._allowed_shout_formids = None
        else:
            self._allowed_shout_formids = self._normalize_shout_formids(formids)

        self._apply_allowed_shout_formids()

    def set_allowed_shout_entries(self, entries: list[tuple[str, str, str, str]] | None) -> None:
        """Accept (plugin, formid, name, editorID) entries from the pipe."""
        self._allowed_shout_entries = entries
        self._allowed_shout_formids = None if entries is None else self._build_allowed_shout_formids_from_entries(entries)
        self._apply_allowed_shout_entries()

    def set_allowed_power_entries(self, entries: list[tuple[str, str]] | None) -> None:
        if not entries:
            self._allowed_power_entries = None
            self._power_phrase_to_formid = None
            self._vosk_power_grammar_json = None
            self._vosk_power_rec = None
            return

        if self._whisper_commands_enabled():
            mapping, _phrases = self._build_whisper_command_mapping(entries)
            self._allowed_power_entries = list(entries)
            self._power_phrase_to_formid = mapping
            self._vosk_power_grammar_json = None
            self._vosk_power_rec = None
            return

        mapping: dict[str, str] = {}
        phrases: list[str] = []

        for formid, name in entries:
            formid_hex = self._normalize_shout_formid(formid)
            phrase = matching.normalize(str(name or ""))
            if not formid_hex or not phrase:
                continue
            if phrase in mapping:
                continue
            mapping[phrase] = formid_hex
            phrases.append(phrase)

        if not phrases:
            self._allowed_power_entries = None
            self._power_phrase_to_formid = None
            self._vosk_power_grammar_json = None
            self._vosk_power_rec = None
            return

        self._allowed_power_entries = list(entries)
        self._power_phrase_to_formid = mapping
        self._vosk_power_grammar_json = json.dumps(phrases, ensure_ascii=False)
        self._vosk_power_rec = None

        try:
            from vosk import KaldiRecognizer
            model = self._ensure_vosk_items()
            if self._vosk_power_grammar_json and self._vosk_power_grammar_json != "[]":
                self._vosk_power_rec = KaldiRecognizer(model, SR, self._vosk_power_grammar_json)
        except Exception as e:
            log_warn(f"[POWER][WARN] failed to init vosk power grammar: {e}")

    def _build_item_grammar(self, entries: list[tuple[str, str]] | None, kind: str) -> tuple[dict[str, str] | None, str | None, object]:
        """Build vosk grammar for item entries. Returns (phrase_to_formid, grammar_json, vosk_rec)."""
        if not entries:
            return None, None, None

        if self._whisper_commands_enabled():
            mapping, _phrases = self._build_whisper_command_mapping(entries)
            return mapping, None, None

        mapping: dict[str, str] = {}
        phrases: list[str] = []

        for formid, name in entries:
            formid_hex = self._normalize_shout_formid(formid)
            phrase = matching.normalize(str(name or ""))
            if not formid_hex or not phrase:
                continue
            if phrase in mapping:
                continue
            mapping[phrase] = formid_hex
            phrases.append(phrase)

        if not phrases:
            return None, None, None

        grammar_json = json.dumps(phrases, ensure_ascii=False)
        vosk_rec = None

        try:
            from vosk import KaldiRecognizer
            model = self._ensure_vosk_items()
            if grammar_json and grammar_json != "[]":
                vosk_rec = KaldiRecognizer(model, SR, grammar_json)
        except Exception as e:
            log_warn(f"[{kind.upper()}][WARN] failed to init vosk grammar: {e}")

        return mapping, grammar_json, vosk_rec

    def set_allowed_weapons_entries(self, entries: list[tuple[str, str]] | None) -> None:
        mapping, grammar_json, rec = self._build_item_grammar(entries, "weapon")
        self._allowed_weapon_entries = list(entries) if entries else None
        self._weapon_phrase_to_formid = mapping
        self._vosk_weapon_grammar_json = grammar_json
        self._vosk_weapon_rec = rec

    def set_allowed_spells_entries(self, entries: list[tuple[str, str]] | None) -> None:
        mapping, grammar_json, rec = self._build_item_grammar(entries, "spell")
        self._allowed_spell_entries = list(entries) if entries else None
        self._spell_phrase_to_formid = mapping
        self._vosk_spell_grammar_json = grammar_json
        self._vosk_spell_rec = rec

    def set_allowed_potions_entries(self, entries: list[tuple[str, str]] | None) -> None:
        mapping, grammar_json, rec = self._build_item_grammar(entries, "potion")
        self._allowed_potion_entries = list(entries) if entries else None
        self._potion_phrase_to_formid = mapping
        self._vosk_potion_grammar_json = grammar_json
        self._vosk_potion_rec = rec

    def _entries_to_phrases(self, entries: list[tuple[str, str]] | None) -> list[str]:
        if not entries:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for _, name in entries:
            raw = str(name or "").strip()
            if not raw:
                continue
            key = matching.normalize(raw)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(raw)
        return out

    def _command_mappings(self) -> dict[str, dict[str, str]]:
        return {
            "power": self._power_phrase_to_formid or {},
            "weapon": self._weapon_phrase_to_formid or {},
            "spell": self._spell_phrase_to_formid or {},
            "potion": self._potion_phrase_to_formid or {},
        }

    def _attempted_command_categories(self, enabled_categories: list[str]) -> list[str]:
        mappings = self._command_mappings()
        attempted: list[str] = []
        for kind in enabled_categories:
            if kind in mappings and mappings[kind]:
                attempted.append(kind)
        return attempted

    def _match_phrase_mapping(
        self,
        text: str,
        phrase_to_formid: dict[str, str] | None,
    ) -> tuple[str, float, str] | None:
        norm = matching.normalize(text)
        formid_hex = (phrase_to_formid or {}).get(norm)
        if formid_hex:
            return (formid_hex, 1.0, text)
        return None

    def _transcribe_command_mapping(self, pcm16: np.ndarray, *, whisper: bool, vosk_rec=None, candidate_phrases: list[str] | None = None) -> tuple[str, dict]:
        if whisper:
            return self._transcribe_whisper_command(pcm16, candidate_phrases=candidate_phrases)
        return self._transcribe_vosk_with_recognizer(vosk_rec, pcm16)

    def _recognize_phrase_mapping(
        self,
        pcm16: np.ndarray,
        phrase_to_formid: dict[str, str] | None,
        *,
        candidate_phrases: list[str] | None = None,
        whisper: bool,
        vosk_rec=None,
        error_tag: str,
    ) -> tuple[str, float, str] | None:
        if pcm16 is None or pcm16.size == 0:
            return None
        if whisper:
            if not phrase_to_formid:
                return None
        elif vosk_rec is None or not phrase_to_formid:
            return None

        try:
            text, _stats = self._transcribe_command_mapping(pcm16, whisper=whisper, vosk_rec=vosk_rec, candidate_phrases=candidate_phrases)
            return self._match_phrase_mapping(text, phrase_to_formid)
        except Exception as e:
            log_error(f"[{error_tag}][ERR] Recognition failed: {e}")
            return None

    def _recognize_phrase_mapping_debug(
        self,
        pcm16: np.ndarray,
        phrase_to_formid: dict[str, str] | None,
        *,
        phrases_count: int,
        candidate_phrases: list[str] | None = None,
        whisper: bool,
        vosk_rec=None,
    ) -> tuple[tuple[str, float, str] | None, dict]:
        dbg: dict = {
            "reason": "init",
            "phrases": int(phrases_count),
        }

        if pcm16 is None or pcm16.size == 0:
            dbg["reason"] = "empty_audio"
            return None, dbg
        if whisper:
            if not phrase_to_formid:
                dbg["reason"] = "no_match"
                return None, dbg
        elif vosk_rec is None or not phrase_to_formid:
            dbg["reason"] = "no_match"
            return None, dbg

        try:
            text, stats = self._transcribe_command_mapping(pcm16, whisper=whisper, vosk_rec=vosk_rec, candidate_phrases=candidate_phrases)
            dbg.update({k: v for k, v in stats.items() if k not in ("t_wav", "t_asr", "t_whisper", "t_total_io", "wav_path")})
            result = self._match_phrase_mapping(text, phrase_to_formid)
            if result is None:
                dbg["text"] = text
                if str(dbg.get("reason") or "") in ("ok", ""):
                    dbg["reason"] = "no_match"
                return None, dbg
            dbg["reason"] = "ok"
            return result, dbg
        except Exception as e:
            dbg["reason"] = "error"
            dbg["error"] = str(e)
            return None, dbg

    def _shout_vosk_model_label(self) -> str:
        name = (self.shouts_vosk_model_name or self.vosk_model_name or "").strip()
        if name:
            return name
        path = (self.shouts_vosk_model_path or self.vosk_model_path or "").strip()
        if path:
            return Path(path).name or path
        return "unknown"

    def _enrich_shout_dbg(self, dbg: dict | None) -> dict:
        if not isinstance(dbg, dict):
            dbg = {}
        dbg.setdefault("grammar_lang", str(self.shouts_lang or "").strip().lower())
        dbg.setdefault("vosk_model", self._shout_vosk_model_label())
        return dbg

    def get_shout_grammar_info(self) -> tuple[int, int, list[str], str, dict[str, list[str]]]:
        recognizer = self._ensure_shout_recognizer()
        lang = str(self.shouts_lang or "").strip().lower()
        if recognizer is None:
            return 0, 0, [], lang, {}
        if hasattr(recognizer, "get_debug_grammar_detail"):
            entries, phrases, per_shout = recognizer.get_debug_grammar_detail()
            return int(entries), int(len(phrases)), list(phrases), lang, dict(per_shout or {})
        if hasattr(recognizer, "get_debug_grammar"):
            entries, phrases = recognizer.get_debug_grammar()
            return int(entries), int(len(phrases)), list(phrases), lang, {}
        return 0, 0, [], lang, {}

    def get_power_grammar_info(self) -> tuple[int, int, list[str]]:
        if self._whisper_commands_enabled():
            return 0, 0, []
        entries = self._allowed_power_entries or []
        phrases = self._entries_to_phrases(entries)
        return int(len(entries)), int(len(phrases)), phrases

    def get_weapon_grammar_info(self) -> tuple[int, int, list[str]]:
        if self._whisper_commands_enabled():
            return 0, 0, []
        entries = self._allowed_weapon_entries or []
        phrases = self._entries_to_phrases(entries)
        return int(len(entries)), int(len(phrases)), phrases

    def get_spell_grammar_info(self) -> tuple[int, int, list[str]]:
        if self._whisper_commands_enabled():
            return 0, 0, []
        entries = self._allowed_spell_entries or []
        phrases = self._entries_to_phrases(entries)
        return int(len(entries)), int(len(phrases)), phrases

    def get_potion_grammar_info(self) -> tuple[int, int, list[str]]:
        if self._whisper_commands_enabled():
            return 0, 0, []
        entries = self._allowed_potion_entries or []
        phrases = self._entries_to_phrases(entries)
        return int(len(entries)), int(len(phrases)), phrases

    def recognize_non_shout_commands_debug(
        self,
        pcm16: np.ndarray,
        enabled_categories: list[str],
    ) -> tuple[tuple[str, str, float, str] | None, dict]:
        dbg: dict = {
            "reason": "init",
            "attempted": [],
        }

        if pcm16 is None or pcm16.size == 0:
            dbg["reason"] = "empty_audio"
            return None, dbg

        if not self._whisper_commands_enabled():
            dbg["reason"] = "whisper_commands_disabled"
            return None, dbg
        mappings = self._command_mappings()
        attempted = self._attempted_command_categories(enabled_categories)
        dbg["attempted"] = attempted
        if not attempted:
            dbg["reason"] = "no_enabled_commands"
            return None, dbg

        try:
            candidate_phrases = self._command_candidate_phrases(attempted)
            text, stats = self._transcribe_whisper_command(pcm16, candidate_phrases=candidate_phrases)
            dbg.update({k: v for k, v in stats.items() if k not in ("t_wav", "t_asr", "t_whisper", "t_total_io", "wav_path")})
            dbg["text"] = text
            norm = matching.normalize(text)
            for kind in attempted:
                formid_hex = mappings[kind].get(norm)
                if formid_hex:
                    dbg["reason"] = "ok"
                    return (kind, formid_hex, 1.0, text), dbg
            if str(dbg.get("reason") or "") in ("ok", ""):
                dbg["reason"] = "no_match"
            return None, dbg
        except Exception as e:
            dbg["reason"] = "error"
            dbg["error"] = str(e)
            return None, dbg

    def _recognize_item(self, pcm16: np.ndarray, vosk_rec, phrase_to_formid: dict[str, str] | None) -> tuple[str, float, str] | None:
        return self._recognize_phrase_mapping(
            pcm16,
            phrase_to_formid,
            whisper=False,
            vosk_rec=vosk_rec,
            error_tag="ITEM",
        )

    def _recognize_item_whisper(
        self,
        pcm16: np.ndarray,
        phrase_to_formid: dict[str, str] | None,
        candidate_phrases: list[str] | None,
    ) -> tuple[str, float, str] | None:
        return self._recognize_phrase_mapping(
            pcm16,
            phrase_to_formid,
            candidate_phrases=candidate_phrases,
            whisper=True,
            error_tag="ITEM",
        )

    def recognize_weapon(self, pcm16: np.ndarray) -> tuple[str, float, str] | None:
        if self._whisper_commands_enabled():
            return self._recognize_item_whisper(
                pcm16,
                self._weapon_phrase_to_formid,
                self._entries_to_phrases(self._allowed_weapon_entries),
            )
        return self._recognize_item(pcm16, self._vosk_weapon_rec, self._weapon_phrase_to_formid)

    def recognize_spell(self, pcm16: np.ndarray) -> tuple[str, float, str] | None:
        if self._whisper_commands_enabled():
            return self._recognize_item_whisper(
                pcm16,
                self._spell_phrase_to_formid,
                self._entries_to_phrases(self._allowed_spell_entries),
            )
        return self._recognize_item(pcm16, self._vosk_spell_rec, self._spell_phrase_to_formid)

    def recognize_potion(self, pcm16: np.ndarray) -> tuple[str, float, str] | None:
        if self._whisper_commands_enabled():
            return self._recognize_item_whisper(
                pcm16,
                self._potion_phrase_to_formid,
                self._entries_to_phrases(self._allowed_potion_entries),
            )
        return self._recognize_item(pcm16, self._vosk_potion_rec, self._potion_phrase_to_formid)

    def recognize_shout(self, pcm16: np.ndarray) -> tuple[str, str, int, float, str] | None:
        if pcm16 is None or pcm16.size == 0:
            return None

        recognizer = self._ensure_shout_recognizer()
        if recognizer is None:
            return None

        try:
            plugin, baseid, power, score, raw_text = recognizer.recognize(pcm16, sr=SR)
            if plugin and baseid and power > 0:
                return (plugin, baseid, power, score, raw_text)
            return None
        except Exception as e:
            log_error(f"[SHOUT][ERR] Recognition failed: {e}")
            return None

    def recognize_power(self, pcm16: np.ndarray) -> tuple[str, float, str] | None:
        if self._whisper_commands_enabled():
            return self._recognize_phrase_mapping(
                pcm16,
                self._power_phrase_to_formid,
                candidate_phrases=self._entries_to_phrases(self._allowed_power_entries),
                whisper=True,
                error_tag="POWER",
            )
        return self._recognize_phrase_mapping(
            pcm16,
            self._power_phrase_to_formid,
            whisper=False,
            vosk_rec=self._vosk_power_rec,
            error_tag="POWER",
        )

    def recognize_power_debug(self, pcm16: np.ndarray) -> tuple[tuple[str, float, str] | None, dict]:
        if self._whisper_commands_enabled():
            return self._recognize_phrase_mapping_debug(
                pcm16,
                self._power_phrase_to_formid,
                phrases_count=len(self._power_phrase_to_formid or {}),
                candidate_phrases=self._entries_to_phrases(self._allowed_power_entries),
                whisper=True,
            )
        return self._recognize_phrase_mapping_debug(
            pcm16,
            self._power_phrase_to_formid,
            phrases_count=len(self._power_phrase_to_formid or {}),
            whisper=False,
            vosk_rec=self._vosk_power_rec,
        )

    def recognize_shout_debug(self, pcm16: np.ndarray) -> tuple[tuple[str, str, int, float, str] | None, dict]:
        if pcm16 is None or pcm16.size == 0:
            return None, self._enrich_shout_dbg({"reason": "empty_audio"})

        recognizer = self._ensure_shout_recognizer()
        if recognizer is None:
            return None, self._enrich_shout_dbg({"reason": "shouts_disabled"})

        try:
            if hasattr(recognizer, "recognize_debug"):
                (plugin, baseid, power, score, raw_text), dbg = recognizer.recognize_debug(pcm16, sr=SR)
            else:
                plugin, baseid, power, score, raw_text = recognizer.recognize(pcm16, sr=SR)
                dbg = {"reason": "ok" if (plugin and baseid and power > 0) else "no_match"}

            dbg = self._enrich_shout_dbg(dbg)

            if plugin and baseid and power > 0:
                return (plugin, baseid, power, float(score), str(raw_text)), dbg
            return None, dbg
        except Exception as e:
            return None, self._enrich_shout_dbg({"reason": "exception", "error": str(e)})

    def warmup_shouts(self) -> bool:
        if not self._shouts_enabled():
            return False
        try:
            self._ensure_shout_recognizer()
            return True
        except Exception as e:
            log_warn(f"[SHOUT][WARN] Warmup failed: {e}")
            return False