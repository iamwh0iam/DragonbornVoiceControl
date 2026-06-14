import os
import sys
import runpy
from pathlib import Path


def _arg_value(name: str) -> str | None:
    lname = name.lower()
    for i, arg in enumerate(sys.argv[:-1]):
        if arg.lower() == lname:
            return sys.argv[i + 1]
    return None


def _has_arg(name: str) -> bool:
    lname = name.lower()
    return any(arg.lower() == lname for arg in sys.argv)


def _runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _compiled_runtime_variant() -> str:
    try:
        from runtime_variant import DVC_RUNTIME_VARIANT
        return str(DVC_RUNTIME_VARIANT or "").strip().lower().replace("_", "-")
    except Exception:
        return ""


def _apply_runtime_variant_env(variant: str) -> None:
    os.environ["DVC_RUNTIME_VARIANT"] = variant

    if variant == "vosk":
        os.environ["DVC_ASR_ENGINE"] = "vosk"
        os.environ.pop("DVC_BACKEND", None)
        os.environ.pop("DVC_CUDA", None)
        return

    if variant == "whisper-cpu":
        os.environ["DVC_ASR_ENGINE"] = "whisper"
        os.environ["DVC_BACKEND"] = "cpu"
        os.environ.pop("DVC_CUDA", None)
        return

    if variant == "whisper-gpu":
        os.environ["DVC_ASR_ENGINE"] = "whisper"
        os.environ["DVC_BACKEND"] = "gpu"
        os.environ.pop("DVC_CUDA", None)
        return

    if variant:
        print(f"[DVC][WARN] Unknown runtime variant: {variant}")


def _run_audio_device_check() -> None:
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        default_in, _default_out = sd.default.device

        if default_in is not None and default_in >= 0:
            d = sd.query_devices(default_in)
            print(f"\nCurrent default input: [{default_in}] {d['name']}")
        else:
            print("\nCurrent default input: (not set)\n")

        print("Hint: in ini you can set [Voice Mode] SetMic=<index>.\n")
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
    finally:
        try:
            input("\nPress Enter...")
        except Exception:
            pass


def main() -> None:
    runtime_dir = _runtime_dir()

    if _has_arg("--check-audio-device"):
        _run_audio_device_check()
        return

    app_arg = _arg_value("--app")
    if not app_arg:
        print("[DVC][FATAL] Missing --app argument. Install/update Dragonborn Voice Control main plugin.")
        sys.exit(1)

    app_zip = Path(app_arg).expanduser().resolve()
    if not app_zip.exists():
        print(f"[DVC][FATAL] app.zip not found: {app_zip}")
        print("Install/update Dragonborn Voice Control main plugin and place it below the runtime mod in MO2.")
        sys.exit(1)

    ini_arg = _arg_value("--ini")

    runtime_variant = _compiled_runtime_variant()
    _apply_runtime_variant_env(runtime_variant)

    os.environ["DVC_RUNTIME_DIR"] = str(runtime_dir)
    os.environ["DVC_APP_ZIP"] = str(app_zip)
    os.environ["DVC_APP_DIR"] = str(app_zip.parent)
    if ini_arg:
        os.environ["DVC_INI"] = str(Path(ini_arg).expanduser().resolve())

    sys.path.insert(0, str(app_zip))
    runpy.run_module("main", run_name="__main__")


if __name__ == "__main__":
    main()
