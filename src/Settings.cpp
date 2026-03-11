#include "PCH.h"

#include "Settings.h"

#include "Common.h"
#include "FavoritesWatcher.h"
#include "Logging.h"
#include "Paths.h"
#include "PipeClient.h"
#include "Runtime.h"
#include "ServerLauncher.h"
#include "VoiceHandle.h"

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

namespace DragonbornVoiceControl
{
    namespace
    {
        constexpr std::uint32_t kSettingsRecord = 'DVCS';
        constexpr std::uint32_t kSettingsRecordVersion = 1;
        constexpr std::size_t kSettingsDataSize = 11 * sizeof(std::uint8_t);
    }

    static std::atomic_bool g_enableVoiceOpenEnabled{ true };
    static std::atomic_bool g_enableVoiceCloseEnabled{ true };
    static std::atomic_bool g_enableDialogueSelectEnabled{ true };
    static std::atomic_bool g_enableVoiceShoutsEnabled{ true };
    static std::atomic_bool g_enablePowersEnabled{ false };
    static std::atomic_bool g_muteShoutVoiceLineEnabled{ true };
    static std::atomic_bool g_enableWeaponsEnabled{ false };
    static std::atomic_bool g_enableSpellsEnabled{ false };
    static std::atomic_bool g_enablePotionsEnabled{ false };
    static std::atomic_bool g_debugEnabled{ false };
    static std::atomic_bool g_saveWavCapturesEnabled{ false };

    static std::mutex g_notifyMutex;
    static double g_notifyTokens = 0.0;
    static double g_notifyLastSec = 0.0;

    void DebugNotify(std::string_view msg)
    {
        if (!g_debugEnabled.load()) {
            return;
        }

        constexpr double kRatePerSec = 8.0;
        constexpr double kBurst = 10.0;

        const double now = GetNowSec();
        std::lock_guard<std::mutex> lg(g_notifyMutex);
        if (g_notifyLastSec <= 0.0) {
            g_notifyLastSec = now;
            g_notifyTokens = kBurst;
        }

        const double dt = std::max(0.0, now - g_notifyLastSec);
        g_notifyLastSec = now;
        g_notifyTokens = std::min(kBurst, g_notifyTokens + dt * kRatePerSec);
        if (g_notifyTokens < 1.0) {
            return;
        }
        g_notifyTokens -= 1.0;

        std::string s = "[DVC] ";
        s += msg;
        RE::DebugNotification(s.c_str());
    }

    bool IsVoiceOpenEnabled() { return g_enableVoiceOpenEnabled.load(); }
    bool IsVoiceCloseEnabled() { return g_enableVoiceCloseEnabled.load(); }
    bool IsDialogueSelectEnabled() { return g_enableDialogueSelectEnabled.load(); }
    bool IsVoiceShoutsEnabled() { return g_enableVoiceShoutsEnabled.load(); }
    bool IsEnablePowersEnabled() { return g_enablePowersEnabled.load(); }
    bool IsMuteShoutVoiceLineEnabled() { return g_muteShoutVoiceLineEnabled.load(); }
    bool IsWeaponsEnabled() { return g_enableWeaponsEnabled.load(); }
    bool IsSpellsEnabled() { return g_enableSpellsEnabled.load(); }
    bool IsPotionsEnabled() { return g_enablePotionsEnabled.load(); }
    bool IsDebugEnabled() { return g_debugEnabled.load(); }
    bool IsSaveWavCapturesEnabled() { return g_saveWavCapturesEnabled.load(); }

    static Settings GetSettingsSnapshot()
    {
        Settings s;
        s.enableVoiceOpen = g_enableVoiceOpenEnabled.load();
        s.enableVoiceClose = g_enableVoiceCloseEnabled.load();
        s.enableDialogueSelect = g_enableDialogueSelectEnabled.load();
        s.enableVoiceShouts = g_enableVoiceShoutsEnabled.load();
        s.enablePowers = g_enablePowersEnabled.load();
        s.muteShoutVoiceLine = g_muteShoutVoiceLineEnabled.load();
        s.enableWeapons = g_enableWeaponsEnabled.load();
        s.enableSpells = g_enableSpellsEnabled.load();
        s.enablePotions = g_enablePotionsEnabled.load();
        s.debug = g_debugEnabled.load();
        s.saveWavCaptures = g_saveWavCapturesEnabled.load();
        return s;
    }

    static void ApplySettings(const Settings& s, bool fromUser)
    {
        const bool oldShouts = g_enableVoiceShoutsEnabled.load();
        const bool oldPowers = g_enablePowersEnabled.load();
        const bool oldWeapons = g_enableWeaponsEnabled.load();
        const bool oldSpells = g_enableSpellsEnabled.load();
        const bool oldPotions = g_enablePotionsEnabled.load();

        g_enableVoiceOpenEnabled.store(s.enableVoiceOpen);
        g_enableVoiceCloseEnabled.store(s.enableVoiceClose);
        g_enableDialogueSelectEnabled.store(s.enableDialogueSelect);
        g_enableVoiceShoutsEnabled.store(s.enableVoiceShouts);
        g_enablePowersEnabled.store(s.enablePowers);
        g_muteShoutVoiceLineEnabled.store(s.muteShoutVoiceLine);
        g_enableWeaponsEnabled.store(s.enableWeapons);
        g_enableSpellsEnabled.store(s.enableSpells);
        g_enablePotionsEnabled.store(s.enablePotions);
        g_debugEnabled.store(s.debug);
        g_saveWavCapturesEnabled.store(s.saveWavCaptures);

        if (IsGameLoaded()) {
            PipeClient::Get().SendConfigOpen(s.enableVoiceOpen);
            PipeClient::Get().SendConfigClose(s.enableVoiceClose);
            PipeClient::Get().SendConfigDialogueSelect(s.enableDialogueSelect);
            PipeClient::Get().SendConfigShouts(s.enableVoiceShouts);
            PipeClient::Get().SendConfigPowers(s.enablePowers);
            PipeClient::Get().SendConfigDebug(s.debug);
            PipeClient::Get().SendConfigSaveWav(s.saveWavCaptures);
            PipeClient::Get().SendConfigWeapons(s.enableWeapons);
            PipeClient::Get().SendConfigSpells(s.enableSpells);
            PipeClient::Get().SendConfigPotions(s.enablePotions);
        }

        // Clear disabled categories immediately
        if (IsGameLoaded()) {
            if (!s.enableWeapons && oldWeapons) PipeClient::Get().SendWeaponsAllowed({});
            if (!s.enableSpells && oldSpells)   PipeClient::Get().SendSpellsAllowed({});
            if (!s.enablePotions && oldPotions) PipeClient::Get().SendPotionsAllowed({});
        }

        // Full rescan whenever any relevant setting changed
        if (fromUser) {
            bool needRescan = false;
            if (s.enableVoiceShouts != oldShouts)       needRescan = true;
            if (s.enablePowers != oldPowers)      needRescan = true;
            if (s.enableWeapons != oldWeapons)          needRescan = true;
            if (s.enableSpells != oldSpells)            needRescan = true;
            if (s.enablePotions != oldPotions)          needRescan = true;

            if (needRescan) {
                SKSE::GetTaskInterface()->AddTask([] {
                    ScanAllFavorites(true);
                    RefreshVoiceCommandState();
                });
            } else if (IsGameLoaded()) {
                RefreshVoiceCommandState();
            }
        } else if (IsGameLoaded()) {
            RefreshVoiceCommandState();
        }
    }

    void ResetToDefaultsForNewGame()
    {
        ApplySettings(Settings{}, false);
    }

    void SaveSettings(SKSE::SerializationInterface* serde)
    {
        if (!serde) {
            return;
        }

        Settings s = GetSettingsSnapshot();

        if (!serde->OpenRecord(kSettingsRecord, kSettingsRecordVersion)) {
            LogLine("[SKSE][SER][WARN] OpenRecord failed for settings");
            return;
        }

        auto writeBool = [&](bool v) {
            std::uint8_t b = v ? 1u : 0u;
            return serde->WriteRecordData(&b, sizeof(b));
        };

        if (!(writeBool(s.enableVoiceOpen) &&
              writeBool(s.enableVoiceClose) &&
              writeBool(s.enableDialogueSelect) &&
              writeBool(s.enableVoiceShouts) &&
              writeBool(s.enablePowers) &&
              writeBool(s.muteShoutVoiceLine) &&
              writeBool(s.enableWeapons) &&
              writeBool(s.enableSpells) &&
              writeBool(s.enablePotions) &&
              writeBool(s.debug) &&
              writeBool(s.saveWavCaptures))) {
            LogLine("[SKSE][SER][WARN] Failed writing settings record");
        }
    }

    void LoadSettings(SKSE::SerializationInterface* serde)
    {
        if (!serde) {
            return;
        }

        std::uint32_t type = 0;
        std::uint32_t version = 0;
        std::uint32_t length = 0;

        while (serde->GetNextRecordInfo(type, version, length)) {
            if (type != kSettingsRecord) {
                if (length > 0) {
                    std::vector<std::uint8_t> scratch(length);
                    serde->ReadRecordData(scratch.data(), length);
                }
                continue;
            }

            if (version != kSettingsRecordVersion) {
                if (length > 0) {
                    std::vector<std::uint8_t> scratch(length);
                    serde->ReadRecordData(scratch.data(), length);
                }
                LogLine("[SKSE][SER][WARN] Unsupported settings version");
                continue;
            }

            Settings s;

            auto readBool = [&](bool& v) {
                std::uint8_t b = 0u;
                if (!serde->ReadRecordData(&b, sizeof(b))) {
                    return false;
                }
                v = b != 0u;
                return true;
            };

            if (!(readBool(s.enableVoiceOpen) &&
                  readBool(s.enableVoiceClose) &&
                  readBool(s.enableDialogueSelect) &&
                  readBool(s.enableVoiceShouts) &&
                  readBool(s.enablePowers) &&
                  readBool(s.muteShoutVoiceLine) &&
                  readBool(s.enableWeapons) &&
                  readBool(s.enableSpells) &&
                  readBool(s.enablePotions) &&
                  readBool(s.debug) &&
                  readBool(s.saveWavCaptures))) {
                LogLine("[SKSE][SER][WARN] Failed reading settings record");
                return;
            }

            if (length > kSettingsDataSize) {
                const std::uint32_t remaining = length - static_cast<std::uint32_t>(kSettingsDataSize);
                if (remaining > 0) {
                    std::vector<std::uint8_t> scratch(remaining);
                    serde->ReadRecordData(scratch.data(), remaining);
                }
            }

            ApplySettings(s, false);
        }
    }

    static bool Pap_GetEnableVoiceOpen(RE::StaticFunctionTag*) { return g_enableVoiceOpenEnabled.load(); }
    static bool Pap_GetEnableVoiceClose(RE::StaticFunctionTag*) { return g_enableVoiceCloseEnabled.load(); }
    static bool Pap_GetEnableDialogueSelect(RE::StaticFunctionTag*) { return g_enableDialogueSelectEnabled.load(); }
    static bool Pap_GetEnableVoiceShouts(RE::StaticFunctionTag*) { return g_enableVoiceShoutsEnabled.load(); }
    static bool Pap_GetEnablePowers(RE::StaticFunctionTag*) { return g_enablePowersEnabled.load(); }
    static bool Pap_GetMuteShoutVoiceLine(RE::StaticFunctionTag*) { return g_muteShoutVoiceLineEnabled.load(); }
    static bool Pap_GetEnableWeapons(RE::StaticFunctionTag*) { return g_enableWeaponsEnabled.load(); }
    static bool Pap_GetEnableSpells(RE::StaticFunctionTag*) { return g_enableSpellsEnabled.load(); }
    static bool Pap_GetEnablePotions(RE::StaticFunctionTag*) { return g_enablePotionsEnabled.load(); }
    static bool Pap_GetDebug(RE::StaticFunctionTag*) { return g_debugEnabled.load(); }
    static bool Pap_GetSaveWavCaptures(RE::StaticFunctionTag*) { return g_saveWavCapturesEnabled.load(); }

    static void Pap_SetEnableVoiceOpen(RE::StaticFunctionTag*, bool v)
    {
        auto s = GetSettingsSnapshot();
        s.enableVoiceOpen = v;
        ApplySettings(s, true);
        if (!v) {
            PipeClient::Get().SendListen(false);
        }
    }

    static void Pap_SetEnableVoiceClose(RE::StaticFunctionTag*, bool v)
    {
        auto s = GetSettingsSnapshot();
        s.enableVoiceClose = v;
        ApplySettings(s, true);
    }

    static void Pap_SetEnableDialogueSelect(RE::StaticFunctionTag*, bool v)
    {
        auto s = GetSettingsSnapshot();
        s.enableDialogueSelect = v;
        ApplySettings(s, true);
    }

    static void Pap_SetEnableVoiceShouts(RE::StaticFunctionTag*, bool v)
    {
        auto s = GetSettingsSnapshot();
        s.enableVoiceShouts = v;
        ApplySettings(s, true);
    }

    static void Pap_SetEnablePowers(RE::StaticFunctionTag*, bool v)
    {
        auto s = GetSettingsSnapshot();
        s.enablePowers = v;
        ApplySettings(s, true);
    }

    static void Pap_SetMuteShoutVoiceLine(RE::StaticFunctionTag*, bool v)
    {
        auto s = GetSettingsSnapshot();
        s.muteShoutVoiceLine = v;
        ApplySettings(s, true);
    }

    static void Pap_SetDebug(RE::StaticFunctionTag*, bool v)
    {
        auto s = GetSettingsSnapshot();
        s.debug = v;
        ApplySettings(s, true);
    }

    static void Pap_SetSaveWavCaptures(RE::StaticFunctionTag*, bool v)
    {
        auto s = GetSettingsSnapshot();
        s.saveWavCaptures = v;
        ApplySettings(s, true);
    }

    static void Pap_SetEnableWeapons(RE::StaticFunctionTag*, bool v)
    {
        auto s = GetSettingsSnapshot();
        s.enableWeapons = v;
        ApplySettings(s, true);
    }

    static void Pap_SetEnableSpells(RE::StaticFunctionTag*, bool v)
    {
        auto s = GetSettingsSnapshot();
        s.enableSpells = v;
        ApplySettings(s, true);
    }

    static void Pap_SetEnablePotions(RE::StaticFunctionTag*, bool v)
    {
        auto s = GetSettingsSnapshot();
        s.enablePotions = v;
        ApplySettings(s, true);
    }

    static void Pap_RestartServer(RE::StaticFunctionTag*)
    {
        auto dataDir = GetDataDirFromPlugin();
        auto iniPath = GetIniPathFromPlugin();

        ServerLauncher::Get().Stop();
        bool ok = ServerLauncher::Get().StartFromIni(dataDir, iniPath);

        LogLine(std::string("[MCM] RestartServer=") + (ok ? "OK" : "FAIL"));
        DebugNotify(std::string("MCM: RestartServer=") + (ok ? "OK" : "FAIL"));
    }

    bool RegisterPapyrus(RE::BSScript::IVirtualMachine* vm)
    {
        if (!vm) {
            return false;
        }

        constexpr auto kClass = "DragonbornVoiceControlMCM";

        vm->RegisterFunction("GetEnableVoiceOpen", kClass, Pap_GetEnableVoiceOpen);
        vm->RegisterFunction("GetEnableVoiceClose", kClass, Pap_GetEnableVoiceClose);
        vm->RegisterFunction("GetEnableDialogueSelect", kClass, Pap_GetEnableDialogueSelect);
        vm->RegisterFunction("GetEnableVoiceShouts", kClass, Pap_GetEnableVoiceShouts);
        vm->RegisterFunction("GetEnablePowers", kClass, Pap_GetEnablePowers);
        vm->RegisterFunction("GetMuteShoutVoiceLine", kClass, Pap_GetMuteShoutVoiceLine);
        vm->RegisterFunction("GetEnableWeapons", kClass, Pap_GetEnableWeapons);
        vm->RegisterFunction("GetEnableSpells", kClass, Pap_GetEnableSpells);
        vm->RegisterFunction("GetEnablePotions", kClass, Pap_GetEnablePotions);
        vm->RegisterFunction("GetDebug", kClass, Pap_GetDebug);
        vm->RegisterFunction("GetSaveWavCaptures", kClass, Pap_GetSaveWavCaptures);

        vm->RegisterFunction("SetEnableVoiceOpen", kClass, Pap_SetEnableVoiceOpen);
        vm->RegisterFunction("SetEnableVoiceClose", kClass, Pap_SetEnableVoiceClose);
        vm->RegisterFunction("SetEnableDialogueSelect", kClass, Pap_SetEnableDialogueSelect);
        vm->RegisterFunction("SetEnableVoiceShouts", kClass, Pap_SetEnableVoiceShouts);
        vm->RegisterFunction("SetEnablePowers", kClass, Pap_SetEnablePowers);
        vm->RegisterFunction("SetMuteShoutVoiceLine", kClass, Pap_SetMuteShoutVoiceLine);
        vm->RegisterFunction("SetEnableWeapons", kClass, Pap_SetEnableWeapons);
        vm->RegisterFunction("SetEnableSpells", kClass, Pap_SetEnableSpells);
        vm->RegisterFunction("SetEnablePotions", kClass, Pap_SetEnablePotions);
        vm->RegisterFunction("SetDebug", kClass, Pap_SetDebug);
        vm->RegisterFunction("SetSaveWavCaptures", kClass, Pap_SetSaveWavCaptures);
        vm->RegisterFunction("RestartServer", kClass, Pap_RestartServer);

        return true;
    }
}
