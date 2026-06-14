# Dragonborn Voice Control

Say It – Dragonborn Voice Control is an SKSE/CommonLibSSE-NG plugin for Skyrim SE/AE/VR that adds voice commands (dialogue start/close/selection, shouts, powers, spells, weapons, potions) via a local IPC-connected runtime. DVCRuntime is an embedded/isolated Python server using Vosk or faster-whisper for on-device speech recognition.

## Build

Configure:

```sh
cmake --preset release
```

Core targets:

```sh
cmake --build --preset release --target DragonbornVoiceControl
cmake --build --preset release --target papyrus
cmake --build --preset release --target stage-mod
```

Runtime targets (PyInstaller). Includes a patched bootloader to resolve the real executable path under MO2/USVFS for onedir apps (fixes `sys._MEIPASS` base path) — if build runtimes without it, auto-launch runtime won’t start with the game client and need to start DVCRuntime manually. To enable the fix, build pyinstaller-bootloader-usvfs first, then build the runtime targets:

```sh
cmake --build --preset release --target pyinstaller-bootloader-usvfs
cmake --build --preset release --target runtime-app
cmake --build --preset release --target runtime-vosk
cmake --build --preset release --target runtime-whisper-cpu
cmake --build --preset release --target runtime-whisper-gpu
```

## Outputs

- `build/release/_mod/...`
- `build/release/runtime_app/...`
- `build/release/runtime/<variant>/dist`
