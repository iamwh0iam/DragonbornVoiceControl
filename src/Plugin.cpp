#include "PCH.h"

#include "Dialogue.h"
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

        switch (a_msg->type) {
            case SKSE::MessagingInterface::kPreLoadGame:
            {
                DragonbornVoiceControl::BeginSaveLoading();
                break;
            }
            case SKSE::MessagingInterface::kNewGame:
            {
                DragonbornVoiceControl::BeginSaveLoading();
                DragonbornVoiceControl::ResetToDefaultsForNewGame();
                DragonbornVoiceControl::RequestSaveReadySync("NewGame");
                break;
            }
            case SKSE::MessagingInterface::kPostLoadGame:
            {
                DragonbornVoiceControl::RequestSaveReadySync("PostLoadGame");
                break;
            }
            case SKSE::MessagingInterface::kDataLoaded:
            {
                DragonbornVoiceControl::LogDebug("[SKSE][MSG] DataLoaded");
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

    DragonbornVoiceControl::LogDebug("[PLUGIN] Plugin loaded");

    auto messaging = SKSE::GetMessagingInterface();
    if (messaging) {
        messaging->RegisterListener(OnSKSEMessage);
        DragonbornVoiceControl::LogDebug("[SKSE] Messaging listener registered");
    } else {
        DragonbornVoiceControl::LogLine("[SKSE][WARN] MessagingInterface not available");
    }

    if (auto papyrus = SKSE::GetPapyrusInterface(); papyrus) {
        papyrus->Register(DragonbornVoiceControl::RegisterPapyrus);
        DragonbornVoiceControl::LogDebug("[SKSE] Papyrus registration requested");
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
    DragonbornVoiceControl::LogDebug("[DVC_SERVER] client started");

    DragonbornVoiceControl::RegisterDialogueWatcher();
    DragonbornVoiceControl::RegisterMenuGateWatcher();

    DragonbornVoiceControl::StartPollThread();

    std::atexit([] {
        DragonbornVoiceControl::StopPollThread();
        PipeClient::Get().Stop();
        ServerLauncher::Get().Stop();
    });

    return true;
}
