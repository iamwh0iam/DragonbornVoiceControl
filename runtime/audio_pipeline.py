from __future__ import annotations

import os
import time
from dataclasses import dataclass
from collections import deque

import numpy as np
import sounddevice as sd
import keyboard

from config import ServerConfig
from log_utils import setup_timestamped_print, log_warn

setup_timestamped_print()


SR = 16000


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


def _env_optional_float(key: str, default: float | None) -> float | None:
    value = os.environ.get(key)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except Exception:
        return default


@dataclass
class VADConfig:
    frame: int
    start_ms: int
    end_sil_ms: int
    max_utt_sec: float
    min_utt_sec: float
    max_wait_sec: float
    thr: float


class SileroStreamVAD:
    def __init__(self, sr: int = SR):
        import torch

        self.torch = torch
        self.sr = sr
        self.model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            trust_repo=True,
        )
        self.reset()

    def reset(self) -> None:
        try:
            self.model.reset_states()
        except Exception:
            pass

    def speech_prob(self, pcm16_fixed: np.ndarray) -> float:
        x = pcm16_fixed.astype(np.float32) / 32768.0
        t = self.torch.from_numpy(x)
        with self.torch.no_grad():
            p = self.model(t, self.sr)
        try:
            return float(p.item())
        except Exception:
            return float(p)

    def is_speech_frame(self, pcm16_fixed: np.ndarray, thr: float) -> bool:
        return self.speech_prob(pcm16_fixed) >= thr


def _record_ptt_device(seconds: float, *, device=None, sr: int = SR) -> np.ndarray:
    audio = sd.rec(
        int(seconds * sr),
        samplerate=sr,
        channels=1,
        dtype="int16",
        device=device,
    )
    sd.wait()
    return audio.reshape(-1)


def _ptt_meta(t_cap0: float, t_cap1: float, samples: int, sr: int = SR) -> dict:
    return {"t_wait": 0.0, "t_vad": (t_cap1 - t_cap0), "utt_sec": samples / sr, "tail_sil_ms": 0.0}


def _record_ptt_until_release(hotkey: str, should_abort, *, device=None, sr: int = SR):
    frames: list[np.ndarray] = []
    t_cap0 = time.perf_counter()

    with sd.InputStream(
        samplerate=sr,
        channels=1,
        dtype="int16",
        blocksize=2048,
        device=device,
    ) as stream:
        while keyboard.is_pressed(hotkey):
            if should_abort():
                t_now = time.perf_counter()
                return None, _ptt_meta(t_cap0, t_now, 0, sr), "pipe"

            pcm = _read_stream_block(stream)
            if pcm is None:
                time.sleep(0.001)
                continue
            frames.append(pcm.copy())

    t_cap1 = time.perf_counter()
    if not frames:
        return None, _ptt_meta(t_cap0, t_cap1, 0, sr), "no_speech"

    pcm16 = np.concatenate(frames)
    return pcm16, _ptt_meta(t_cap0, t_cap1, pcm16.size, sr), "ok"


def _frame_limits(frame: int, sr: int, start_ms: int, end_sil_ms: int, max_utt_sec: float, min_utt_sec: float, max_wait_sec: float):
    frame_ms = (frame / sr) * 1000.0
    need_start = max(1, int(start_ms / frame_ms))
    need_end = max(1, int(end_sil_ms / frame_ms))
    max_frames = int(max_utt_sec / (frame / sr))
    min_frames = int(min_utt_sec / (frame / sr))
    max_wait_frames = int(max_wait_sec / (frame / sr))
    return frame_ms, need_start, need_end, max_frames, min_frames, max_wait_frames


@dataclass
class _VADRecordState:
    frames: list[np.ndarray]
    started: bool
    speech_streak: int
    silence_streak: int
    waited_frames: int
    buf: np.ndarray
    t_listen0: float
    t_start: float | None


def _vad_meta(include_tail_sil: bool, silence_streak: int, frame_ms: float, t_wait: float, t_vad: float, utt_sec: float):
    base = {"t_wait": t_wait, "t_vad": t_vad, "utt_sec": utt_sec}
    if include_tail_sil:
        base["tail_sil_ms"] = silence_streak * frame_ms
    return base


def _vad_reached_stop(frames_len: int, max_frames: int, silence_streak: int, need_end: int, min_frames: int) -> bool:
    if frames_len >= max_frames:
        return True
    return silence_streak >= need_end and frames_len >= min_frames


def _process_vad_frame(
    *,
    state: _VADRecordState,
    cur: np.ndarray,
    vad: SileroStreamVAD,
    thr: float,
    need_start: int,
    need_end: int,
    max_frames: int,
    min_frames: int,
    max_wait_frames: int,
    pre_roll_buf,
    pre_roll_frames: int,
):
    state.waited_frames += 1
    if (not state.started) and state.waited_frames >= max_wait_frames:
        return "no_speech"

    speech = vad.is_speech_frame(cur, thr=thr)
    if not state.started:
        if pre_roll_frames > 0:
            pre_roll_buf.append(cur.copy())
        state.speech_streak = (state.speech_streak + 1) if speech else 0
        if state.speech_streak >= need_start:
            state.started = True
            state.t_start = time.perf_counter()
            if pre_roll_frames > 0 and len(pre_roll_buf) > 0:
                state.frames.extend(list(pre_roll_buf))
                pre_roll_buf.clear()
            else:
                state.frames.append(cur.copy())
        return "continue"

    state.frames.append(cur.copy())
    state.silence_streak = 0 if speech else (state.silence_streak + 1)
    if _vad_reached_stop(len(state.frames), max_frames, state.silence_streak, need_end, min_frames):
        return "stop"
    return "continue"


def _read_stream_block(stream) -> np.ndarray | None:
    pcm, _ = stream.read(2048)
    if pcm is None or pcm.size == 0:
        return None
    return pcm.reshape(-1)


def _abort_if_needed(should_abort, abort_payload):
    if should_abort():
        return abort_payload("pipe")
    return None


def _consume_vad_buffer(
    *,
    state: _VADRecordState,
    frame: int,
    vad: SileroStreamVAD,
    thr: float,
    need_start: int,
    need_end: int,
    max_frames: int,
    min_frames: int,
    max_wait_frames: int,
    pre_roll_buf,
    pre_roll_frames: int,
    should_abort,
    abort_payload,
):
    status = "continue"
    while state.buf.size >= frame and status == "continue":
        aborted = _abort_if_needed(should_abort, abort_payload)
        if aborted is not None:
            return status, aborted

        cur = state.buf[:frame]
        state.buf = state.buf[frame:]

        status = _process_vad_frame(
            state=state,
            cur=cur,
            vad=vad,
            thr=thr,
            need_start=need_start,
            need_end=need_end,
            max_frames=max_frames,
            min_frames=min_frames,
            max_wait_frames=max_wait_frames,
            pre_roll_buf=pre_roll_buf,
            pre_roll_frames=pre_roll_frames,
        )

        if status == "no_speech":
            return status, abort_payload("no_speech")

    return status, None


def _record_vad_generic(
    *,
    vad: SileroStreamVAD,
    frame: int,
    thr: float,
    need_start: int,
    need_end: int,
    max_frames: int,
    min_frames: int,
    max_wait_frames: int,
    pre_roll_ms: int,
    should_abort,
    include_tail_sil: bool,
    device,
    sr: int = SR,
):
    frame_ms = (frame / sr) * 1000.0
    pre_roll_frames = max(0, int(pre_roll_ms / frame_ms))
    pre_roll_buf = deque(maxlen=max(1, pre_roll_frames)) if pre_roll_frames > 0 else deque(maxlen=1)
    vad.reset()

    state = _VADRecordState(
        frames=[],
        started=False,
        speech_streak=0,
        silence_streak=0,
        waited_frames=0,
        buf=np.zeros((0,), dtype=np.int16),
        t_listen0=time.perf_counter(),
        t_start=None,
    )

    def _meta(t_wait: float, t_vad: float, utt_sec: float):
        return _vad_meta(include_tail_sil, state.silence_streak, frame_ms, t_wait, t_vad, utt_sec)

    def _abort_payload(reason: str):
        t_now = time.perf_counter()
        return None, _meta((t_now - state.t_listen0), 0.0, 0.0), reason

    status = "continue"

    with sd.InputStream(
        samplerate=sr,
        channels=1,
        dtype="int16",
        blocksize=2048,
        device=device,
    ) as stream:
        while status == "continue":
            aborted = _abort_if_needed(should_abort, _abort_payload)
            if aborted is not None:
                return aborted

            pcm = _read_stream_block(stream)
            if pcm is None:
                time.sleep(0.001)
                continue

            state.buf = np.concatenate([state.buf, pcm])
            status, payload = _consume_vad_buffer(
                state=state,
                frame=frame,
                vad=vad,
                thr=thr,
                need_start=need_start,
                need_end=need_end,
                max_frames=max_frames,
                min_frames=min_frames,
                max_wait_frames=max_wait_frames,
                pre_roll_buf=pre_roll_buf,
                pre_roll_frames=pre_roll_frames,
                should_abort=should_abort,
                abort_payload=_abort_payload,
            )
            if payload is not None:
                return payload

    t_end = time.perf_counter()
    t_wait = (state.t_start - state.t_listen0) if (state.t_start is not None) else (t_end - state.t_listen0)
    t_vad = (t_end - state.t_start) if (state.t_start is not None) else 0.0

    if len(state.frames) < min_frames:
        return None, _meta(t_wait, t_vad, len(state.frames) * (frame / sr)), "no_speech"

    pcm16 = np.concatenate(state.frames)
    return pcm16, _meta(t_wait, t_vad, pcm16.size / sr), "ok"


def _record_vad_stream(vad: SileroStreamVAD, cfg: VADConfig, should_abort, *, device=None, sr: int = SR):
    _frame_ms, need_start, need_end, max_frames, min_frames, max_wait_frames = _frame_limits(
        cfg.frame,
        sr,
        cfg.start_ms,
        cfg.end_sil_ms,
        cfg.max_utt_sec,
        cfg.min_utt_sec,
        cfg.max_wait_sec,
    )
    return _record_vad_generic(
        vad=vad,
        frame=cfg.frame,
        thr=cfg.thr,
        need_start=need_start,
        need_end=need_end,
        max_frames=max_frames,
        min_frames=min_frames,
        max_wait_frames=max_wait_frames,
        pre_roll_ms=0,
        should_abort=should_abort,
        include_tail_sil=True,
        device=device,
        sr=sr,
    )


def _record_vad_open(
    vad: SileroStreamVAD,
    vad_frame: int,
    vad_thr: float,
    open_end_sil_ms: int,
    open_max_rec_sec: float,
    should_abort,
    *,
    device=None,
    sr: int = SR,
):
    _frame_ms, need_start, need_end, max_frames, min_frames, max_wait_frames = _frame_limits(
        vad_frame,
        sr,
        start_ms=100,
        end_sil_ms=open_end_sil_ms,
        max_utt_sec=open_max_rec_sec,
        min_utt_sec=0.3,
        max_wait_sec=3.0,
    )
    return _record_vad_generic(
        vad=vad,
        frame=vad_frame,
        thr=vad_thr,
        need_start=need_start,
        need_end=need_end,
        max_frames=max_frames,
        min_frames=min_frames,
        max_wait_frames=max_wait_frames,
        pre_roll_ms=0,
        should_abort=should_abort,
        include_tail_sil=False,
        device=device,
        sr=sr,
    )


class AudioPipeline:
    def __init__(self, cfg=None):
        self.cfg = cfg if cfg is not None else ServerConfig()

        cfg_mode = self.cfg.mode
        cfg_hotkey = self.cfg.ptt_key
        cfg_ptt_sec = self.cfg.ptt_sec

        self.mode = _env_str("DVC_VOICE_MODE", str(cfg_mode)).lower()
        self.hotkey = _env_str("DVC_VOICE_MODE_HOTKEY", str(cfg_hotkey)).lower()
        self.ptt_seconds = _env_optional_float("DVC_VOICE_MODE_SECONDS", cfg_ptt_sec)
        self.SetMic = _env_str("DVC_VOICE_MODE_SET_MIC", str(self.cfg.SetMic))

        self.vad_frame = _env_int("DVC_VAD_FRAME", int(self.cfg.vad_frame))
        self.vad_thr = _env_float("DVC_VAD_THR", float(self.cfg.vad_thr))

        self.open_max_rec_sec = _env_float("DVC_OPEN_MAX_REC_SEC", float(self.cfg.open_max_rec_sec))
        self.open_vad_end_sil_ms = _env_int("DVC_OPEN_VAD_END_SIL_MS", int(self.cfg.open_vad_end_sil_ms))

        self._vad_cfg = VADConfig(
            frame=self.vad_frame,
            start_ms=_env_int("DVC_VAD_START_MS", int(self.cfg.vad_start_ms)),
            end_sil_ms=_env_int("DVC_VAD_END_SIL_MS", int(self.cfg.vad_end_sil_ms)),
            max_utt_sec=_env_float("DVC_VAD_MAX_UTT", float(self.cfg.vad_max_utt)),
            min_utt_sec=_env_float("DVC_VAD_MIN_UTT", float(self.cfg.vad_min_utt)),
            max_wait_sec=_env_float("DVC_VAD_MAX_WAIT", float(self.cfg.vad_max_wait)),
            thr=self.vad_thr,
        )
        self.vad_preroll_ms = _env_int(
            "DVC_VAD_PREROLL_MS",
            int(self.cfg.vad_preroll_ms),
        )
        self._vad = None

        self._abort_checker = None
        self.input_device = self._resolve_input_device()

        if self.mode == "vad":
            # Load VAD lazily on first capture call; keep init light.
            pass

    def warmup(self) -> None:
        if self.mode == "vad":
            self._ensure_vad()

    def set_abort_checker(self, fn) -> None:
        self._abort_checker = fn

    def _should_abort(self) -> bool:
        try:
            return bool(self._abort_checker() if self._abort_checker else False)
        except Exception:
            return False

    def _ensure_vad(self) -> SileroStreamVAD:
        if self._vad is None:
            self._vad = SileroStreamVAD(sr=SR)
        return self._vad

    def _resolve_input_device(self):
        set_mic_raw = str(self.SetMic).strip()
        if set_mic_raw:
            try:
                idx = int(set_mic_raw)
                dev = sd.query_devices(idx)
                if int(dev.get("max_input_channels", 0)) > 0:
                    return idx
                log_warn(f"[AUDIO][WARN] Voice Mode.SetMic={idx} is not an input device, fallback to default input")
            except Exception as e:
                log_warn(f"[AUDIO][WARN] invalid Voice Mode.SetMic={set_mic_raw!r}: {e}; fallback to default input")

        try:
            default_in, _ = sd.default.device
            if default_in is not None and int(default_in) >= 0:
                return int(default_in)
        except Exception:
            pass
        return None

    def active_input_device_label(self) -> str:
        try:
            if self.input_device is not None and int(self.input_device) >= 0:
                idx = int(self.input_device)
                d = sd.query_devices(idx)
                return f"[{idx}] {d['name']}"
        except Exception:
            pass
        return "(not set)"

    def capture_for_open(self):
        # Open capture uses VAD-based recording.
        if self.mode != "vad":
            raise RuntimeError("Open capture requires VAD mode")
        vad = self._ensure_vad()
        return _record_vad_open(
            vad,
            vad_frame=self.vad_frame,
            vad_thr=self.vad_thr,
            open_end_sil_ms=self.open_vad_end_sil_ms,
            open_max_rec_sec=self.open_max_rec_sec,
            should_abort=self._should_abort,
            device=self.input_device,
            sr=SR,
        )

    def capture_for_dialogue(self):
        # Returns (pcm16|None, meta, reason)
        if self.mode == "ptt":
            if not keyboard.is_pressed(self.hotkey):
                return None, {"t_wait": 0.0, "t_vad": 0.0, "utt_sec": 0.0, "tail_sil_ms": 0.0}, "no_hotkey"
            if self.ptt_seconds is None:
                return _record_ptt_until_release(
                    self.hotkey,
                    self._should_abort,
                    device=self.input_device,
                    sr=SR,
                )
            time.sleep(0.15)
            t_cap0 = time.perf_counter()
            pcm16 = _record_ptt_device(seconds=self.ptt_seconds, device=self.input_device, sr=SR)
            t_cap1 = time.perf_counter()
            return pcm16, _ptt_meta(t_cap0, t_cap1, pcm16.size), "ok"

        vad = self._ensure_vad()
        return _record_vad_stream_with_preroll(
            vad,
            self._vad_cfg,
            pre_roll_ms=self.vad_preroll_ms,
            should_abort=self._should_abort,
            device=self.input_device,
            sr=SR,
        )


def _record_vad_stream_with_preroll(
    vad: SileroStreamVAD,
    cfg: VADConfig,
    pre_roll_ms: int,
    should_abort,
    *,
    device=None,
    sr: int = SR,
):
    _frame_ms, need_start, need_end, max_frames, min_frames, max_wait_frames = _frame_limits(
        cfg.frame,
        sr,
        cfg.start_ms,
        cfg.end_sil_ms,
        cfg.max_utt_sec,
        cfg.min_utt_sec,
        cfg.max_wait_sec,
    )
    return _record_vad_generic(
        vad=vad,
        frame=cfg.frame,
        thr=cfg.thr,
        need_start=need_start,
        need_end=need_end,
        max_frames=max_frames,
        min_frames=min_frames,
        max_wait_frames=max_wait_frames,
        pre_roll_ms=pre_roll_ms,
        should_abort=should_abort,
        include_tail_sil=True,
        device=device,
        sr=sr,
    )
