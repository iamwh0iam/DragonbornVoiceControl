#include "PCH.h"

#include "Dialogue.h"
#include "FavoritesWatcher.h"
#include "GameLanguage.h"
#include "Logging.h"
#include "Paths.h"
#include "PipeClient.h"
#include "Runtime.h"
#include "ServerLauncher.h"
#include "Settings.h"
#include "VoiceHandle.h"
#include "VoiceTrigger.h"

#include <cstdlib>
#include <optional>

namespace
{
    std::optional<std::string> g_gameLangLogged;

    void TryDetectAndSendGameLanguage()
    {
        auto info = DragonbornVoiceControl::DetectGameLanguage();
        if (info.code.empty()) {
            return;
        }

        if (!g_gameLangLogged.has_value() || g_gameLangLogged.value() != info.code) {
            DragonbornVoiceControl::LogLine(std::string("[LANG] game language detected: ") + info.label);
            g_gameLangLogged = info.code;
        }

        PipeClient::Get().SendGameLanguage(info.code);
    }

    void OnSave(SKSE::SerializationInterface* serde)
    {
        DragonbornVoiceControl::SaveSettings(serde);
    }

    void OnLoad(SKSE::SerializationInterface* serde)
    {
        DragonbornVoiceControl::LoadSettings(serde);
    }

    void OnSKSEMessage(SKSE::MessagingInterface::Message* a_msg)
    {
        if (!a_msg) return;

        const auto syncOnGameLoaded = [](const char* label) {
            DragonbornVoiceControl::SetGameLoaded(true);

            DragonbornVoiceControl::LogLine(std::string("[SKSE][MSG] ") + label);

            PipeClient::Get().SendConfigOpen(DragonbornVoiceControl::IsVoiceOpenEnabled());
            PipeClient::Get().SendConfigClose(DragonbornVoiceControl::IsVoiceCloseEnabled());
            PipeClient::Get().SendConfigDialogueSelect(DragonbornVoiceControl::IsDialogueSelectEnabled());
            PipeClient::Get().SendConfigShouts(DragonbornVoiceControl::IsVoiceShoutsEnabled());
            PipeClient::Get().SendConfigPowers(DragonbornVoiceControl::IsEnablePowersEnabled());
            PipeClient::Get().SendConfigDebug(DragonbornVoiceControl::IsDebugEnabled());
            PipeClient::Get().SendConfigSaveWav(DragonbornVoiceControl::IsSaveWavCapturesEnabled());
            PipeClient::Get().SendConfigWeapons(DragonbornVoiceControl::IsWeaponsEnabled());
            PipeClient::Get().SendConfigSpells(DragonbornVoiceControl::IsSpellsEnabled());
            PipeClient::Get().SendConfigPotions(DragonbornVoiceControl::IsPotionsEnabled());

            SKSE::GetTaskInterface()->AddTask([]() {
                DragonbornVoiceControl::ScanAllFavorites(true);
                DragonbornVoiceControl::RefreshVoiceCommandState();
            });
        };

        switch (a_msg->type) {
            case SKSE::MessagingInterface::kNewGame:
            {
                DragonbornVoiceControl::ResetToDefaultsForNewGame();
                syncOnGameLoaded("NewGame");
                break;
            }
            case SKSE::MessagingInterface::kPostLoadGame:
            {
                syncOnGameLoaded("PostLoadGame");
                break;
            }
            case SKSE::MessagingInterface::kDataLoaded:
            {
                DragonbornVoiceControl::LogLine("[SKSE][MSG] DataLoaded");
                TryDetectAndSendGameLanguage();
                break;
            }
            default:
                break;
        }
    }
}

SKSEPluginLoad(const SKSE::LoadInterface* skse)
{
    DragonbornVoiceControl::SetupLogging(skse);
    SKSE::Init(skse);

    DragonbornVoiceControl::LogLine("[PLUGIN] Plugin loaded");

    auto messaging = SKSE::GetMessagingInterface();
    if (messaging) {
        messaging->RegisterListener(OnSKSEMessage);
        DragonbornVoiceControl::LogLine("[SKSE] Messaging listener registered");
    } else {
        DragonbornVoiceControl::LogLine("[SKSE][WARN] MessagingInterface not available");
    }

    if (auto papyrus = SKSE::GetPapyrusInterface(); papyrus) {
        papyrus->Register(DragonbornVoiceControl::RegisterPapyrus);
        DragonbornVoiceControl::LogLine("[SKSE] Papyrus registration requested");
    } else {
        DragonbornVoiceControl::LogLine("[SKSE][WARN] PapyrusInterface not available");
    }

    if (auto serialization = SKSE::GetSerializationInterface(); serialization) {
        serialization->SetUniqueID('DVCS');
        serialization->SetSaveCallback(OnSave);
        serialization->SetLoadCallback(OnLoad);
    } else {
    }

    {
        auto dataDir = DragonbornVoiceControl::GetDataDirFromPlugin();
        auto iniPath = DragonbornVoiceControl::GetIniPathFromPlugin();

        bool ok = ServerLauncher::Get().StartFromIni(dataDir, iniPath);
        DragonbornVoiceControl::LogLine(std::string("[DVC_SERVER] launch=") + (ok ? "OK" : "FAIL"));
    }

    PipeClient::Get().Start();
    DragonbornVoiceControl::LogLine("[DVC_SERVER] client started");

    DragonbornVoiceControl::RegisterDialogueWatcher();
    DragonbornVoiceControl::RegisterFavoritesWatcher();

    DragonbornVoiceControl::StartPollThread();

    std::atexit([] {
        DragonbornVoiceControl::StopPollThread();
        PipeClient::Get().Stop();
        ServerLauncher::Get().Stop();
    });

    return true;
}
