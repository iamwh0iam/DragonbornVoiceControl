import sounddevice as sd

from log_utils import setup_timestamped_print, log_info

setup_timestamped_print()

def main():
    devices = sd.query_devices()
    default_in, default_out = sd.default.device  # (input, output)
    
    if default_in is not None and default_in >= 0:
        d = sd.query_devices(default_in)
        log_info(f"\nCurrent default input: [{default_in}] {d['name']}")
    else:
        log_info("\nCurrent default input: (not set)\n")
    log_info("Hint: in ini you can set [Voice Mode] SetMic=<index>.\n")
    log_info("Input devices (recording):")

    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            mark = "  <-- default input" if i == default_in else ""
            hostapi = sd.query_hostapis(d["hostapi"])["name"]
            log_info(
                f"[{i}] {d['name']} | hostapi: {hostapi} | "
                f"in_ch: {d['max_input_channels']} | "
                f"default_sr: {d['default_samplerate']}{mark}"
            )

if __name__ == "__main__":
    main()
