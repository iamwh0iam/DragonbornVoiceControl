#include "PCH.h"

#include "Runtime.h"

#include "Common.h"
#include "Dialogue.h"
#include "FavoritesWatcher.h"
#include "Logging.h"
#include "Paths.h"
#include "Settings.h"
#include "ShoutsInternal.h"
#include "VoiceHandle.h"
#include "PipeClient.h"

#include <atomic>
#include <chrono>
#include <cmath>
#include <cwctype>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <mutex>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_set>
#include <utility>

namespace DragonbornVoiceControl
{
    static constexpr int kFocusPollMs = 150;
    static constexpr int kFocusOnDelayMs = 250;
    static constexpr int kFocusGraceMs = 1500;
    static constexpr int kSaveReadyDebounceMs = 1000;
    static constexpr float kFocusMaxDistCm = 300.0f;
    static constexpr float kLookAtCosThreshold = 0.85f;

    static std::atomic_bool g_running{ true };
    static std::thread g_pollThread;
    static std::atomic_uint64_t g_saveSyncRequestGen{ 0 };
    static std::atomic_bool g_saveSyncPending{ false };
    static std::atomic_bool g_saveProbeInFlight{ false };
    static std::atomic_bool g_saveProbeHasResult{ false };
    static std::atomic_bool g_saveProbeReadyResult{ false };
    static std::atomic_uint64_t g_saveProbeResultGen{ 0 };

    static std::atomic_bool g_listenModeActive{ false };
    static RE::ObjectRefHandle g_focusedActorHandle;
    static std::chrono::steady_clock::time_point g_focusAcquiredTime;
    static std::chrono::steady_clock::time_point g_focusLostTime;
    static bool g_hadFocusLastPoll = false;
    static std::string g_lastRecognitionText;
    static std::mutex g_blockingMenuMutex;
    static std::unordered_set<std::string> g_openBlockingMenus;

    static bool HasConfiguredCustomCommands();
    static void RefreshListenState();

    static bool IsBlockingMenuName(std::string_view menuName)
    {
        return menuName == "InventoryMenu"sv ||
               menuName == "MagicMenu"sv ||
               menuName == "FavoritesMenu"sv ||
               menuName == "ContainerMenu"sv ||
               menuName == "BarterMenu"sv ||
               menuName == "GiftMenu"sv ||
               menuName == "Crafting Menu"sv ||
               menuName == "Journal Menu"sv ||
               menuName == "MapMenu"sv ||
               menuName == "Book Menu"sv;
    }

    static void ClearFocusListenState()
    {
        g_listenModeActive.store(false);
        g_hadFocusLastPoll = false;
        g_focusedActorHandle.reset();
    }

    static void StopCaptureForBlockingMenu(std::string_view menuName)
    {
        g_listenModeActive.store(false);
        RefreshListenState();
        LogDebug("[MENU] " + std::string(menuName) + " open, stop capture");
    }

    static std::string FormatScore(float score)
    {
        std::ostringstream out;
        out << std::fixed << std::setprecision(3) << score;
        return out.str();
    }

    static const char* BoolText(bool value)
    {
        return value ? "ON" : "OFF";
    }

    static std::string Quote(const std::string& text)
    {
        std::string out = "\"";
        for (char ch : text) {
            if (ch == '"' || ch == '\\') {
                out.push_back('\\');
            }
            out.push_back(ch);
        }
        out.push_back('"');
        return out;
    }

    static constexpr RE::FormID kPauseResumeSoundFormID = 0x802;
    static constexpr const char* kPluginName = "DragonbornVoiceControl.esp";

    static void PlayPauseResumeSound_MainThread()
    {
        auto* dataHandler = RE::TESDataHandler::GetSingleton();
        if (!dataHandler) {
            return;
        }

        auto* sound = dataHandler->LookupForm<RE::BGSSoundDescriptorForm>(kPauseResumeSoundFormID, kPluginName);
        if (!sound) {
            LogWarn("[SFX] Pause/resume sound not found: DragonbornVoiceControl.esp|0x802");
            return;
        }

        auto* audio = RE::BSAudioManager::GetSingleton();
        if (!audio) {
            return;
        }

        RE::BSSoundHandle handle;
        audio->BuildSoundDataFromDescriptor(handle, sound, 0x10);

        if (auto* player = RE::PlayerCharacter::GetSingleton(); player && player->Get3D()) {
            handle.SetObjectToFollow(player->Get3D());
        }

        handle.Play();
    }

    static void QueuePauseResumeSound()
    {
        if (auto* tasks = SKSE::GetTaskInterface(); tasks) {
            tasks->AddTask([] {
                PlayPauseResumeSound_MainThread();
            });
        }
    }

    static bool IsPauseResumeDebugMessage(const std::string& text)
    {
        return text == "Voice commands disabled" || text == "Voice commands enabled";
    }

    static bool TryExtractRecognitionText(const std::string& msg, std::string& out)
    {
        constexpr std::string_view prefix = "Recognition: \"";
        if (msg.rfind(prefix, 0) != 0 || msg.size() < prefix.size() + 1) {
            return false;
        }

        const auto end = msg.find_last_of('"');
        if (end == std::string::npos || end <= prefix.size()) {
            return false;
        }

        out = msg.substr(prefix.size(), end - prefix.size());
        return true;
    }

    static bool HasAnyVoiceCommandFeaturesEnabled()
    {
        return IsVoiceShoutsEnabled() || IsEnablePowersEnabled() || IsWeaponsEnabled() ||
               IsSpellsEnabled() || IsPotionsEnabled() || IsPauseResumePhrasesEnabled() ||
               (IsKeyConsoleEnabled() && HasConfiguredCustomCommands());
    }

    static bool IsOpenListenWanted()
    {
        return IsGameLoaded() && IsVoiceOpenEnabled() && g_listenModeActive.load() &&
               !IsDialogueOpen() && !IsBlockingMenuOpen();
    }

    static bool IsCommandListenWanted()
    {
        return IsGameLoaded() && HasAnyVoiceCommandFeaturesEnabled() &&
               !IsDialogueOpen() && !IsBlockingMenuOpen();
    }

    static void RefreshListenState()
    {
        if (!IsGameLoaded()) {
            return;
        }

        const bool menuBlocked = IsBlockingMenuOpen();
        const bool openWanted = IsOpenListenWanted();
        const bool commandWanted = IsCommandListenWanted();
        PipeClient::Get().SendBlockingMenuState(menuBlocked);
        PipeClient::Get().SendConfigOpen(openWanted);
        PipeClient::Get().SendConfigShouts(IsVoiceShoutsEnabled());
        PipeClient::Get().SendListen(openWanted || commandWanted);
        SyncShoutContextState();
    }

    static std::wstring TrimCopy(std::wstring value)
    {
        while (!value.empty() && std::iswspace(value.front())) {
            value.erase(value.begin());
        }
        while (!value.empty() && std::iswspace(value.back())) {
            value.pop_back();
        }
        return value;
    }

    static bool HasConfiguredCustomCommands()
    {
        std::wifstream in(GetIniPathFromPlugin());
        if (!in) {
            return false;
        }

        bool inSection = false;
        std::wstring line;
        while (std::getline(in, line)) {
            std::wstring trimmed = TrimCopy(line);
            if (trimmed.empty() || trimmed.front() == L'#' || trimmed.front() == L';') {
                continue;
            }
            if (trimmed.front() == L'[') {
                inSection = trimmed == L"[Custom Commands]";
                continue;
            }
            if (!inSection) {
                continue;
            }

            const auto eq = trimmed.find(L'=');
            if (eq == std::wstring::npos) {
                continue;
            }
            if (!TrimCopy(trimmed.substr(0, eq)).empty() && !TrimCopy(trimmed.substr(eq + 1)).empty()) {
                return true;
            }
        }
        return false;
    }

    static bool CheckSaveReady_MainThread()
    {
        auto* player = RE::PlayerCharacter::GetSingleton();
        auto* dataHandler = RE::TESDataHandler::GetSingleton();
        auto* favorites = RE::MagicFavorites::GetSingleton();
        auto* ui = RE::UI::GetSingleton();
        if (!player || !dataHandler || !favorites || !ui) {
            return false;
        }

        if (ui->GameIsPaused()) {
            return false;
        }

        (void)player->GetInventory();

        for (auto* form : favorites->spells) {
            (void)form;
            break;
        }
        for (auto* form : favorites->hotkeys) {
            (void)form;
            break;
        }

        return true;
    }

    static void RequestSaveReadyProbe(std::uint64_t generation)
    {
        if (g_saveProbeInFlight.exchange(true)) {
            return;
        }

        if (auto* tasks = SKSE::GetTaskInterface(); tasks) {
            tasks->AddTask([generation]() {
                const bool ready = CheckSaveReady_MainThread();
                g_saveProbeReadyResult.store(ready);
                g_saveProbeResultGen.store(generation);
                g_saveProbeHasResult.store(true);
                g_saveProbeInFlight.store(false);
            });
        } else {
            g_saveProbeReadyResult.store(false);
            g_saveProbeResultGen.store(generation);
            g_saveProbeHasResult.store(true);
            g_saveProbeInFlight.store(false);
        }
    }

    static bool IsPlayerInCombat()
    {
        auto player = RE::PlayerCharacter::GetSingleton();
        return player && player->IsInCombat();
    }

    static void SendRuntimeConfigInternal()
    {
        PipeClient::Get().SendConfigOpen(IsOpenListenWanted());
        PipeClient::Get().SendConfigClose(IsVoiceCloseEnabled());
        PipeClient::Get().SendConfigDialogueSelect(IsDialogueSelectEnabled());
        PipeClient::Get().SendConfigShouts(IsVoiceShoutsEnabled());
        PipeClient::Get().SendConfigPowers(IsEnablePowersEnabled());
        PipeClient::Get().SendConfigDebug(IsDebugEnabled());
        PipeClient::Get().SendConfigSaveWav(IsSaveWavCapturesEnabled());
        PipeClient::Get().SendConfigWeapons(IsWeaponsEnabled());
        PipeClient::Get().SendConfigSpells(IsSpellsEnabled());
        PipeClient::Get().SendConfigPotions(IsPotionsEnabled());
        PipeClient::Get().SendConfigPotionsQuickUse(IsQuickUsePotionsEnabled());
        PipeClient::Get().SendConfigPotionsBestPotion(IsUseBestPotionEnabled());
        PipeClient::Get().SendConfigSpecifyHand(IsSpecifyHandEnabled());
        PipeClient::Get().SendConfigQuickEquip(IsQuickEquipEnabled());
        PipeClient::Get().SendConfigKeyConsole(IsKeyConsoleEnabled());
        PipeClient::Get().SendConfigPauseResume(IsPauseResumePhrasesEnabled());

        LogDebug(std::string("[CFG][STATE] Plugin MCM: ") +
                 "select=" + BoolText(IsDialogueSelectEnabled()) +
                 " open=" + BoolText(IsVoiceOpenEnabled()) +
                 " close=" + BoolText(IsVoiceCloseEnabled()) +
                 " shouts=" + BoolText(IsVoiceShoutsEnabled()) +
                 " powers=" + BoolText(IsEnablePowersEnabled()) +
                 " weapons=" + BoolText(IsWeaponsEnabled()) +
                 " spells=" + BoolText(IsSpellsEnabled()) +
                 " potions=" + BoolText(IsPotionsEnabled()) +
                 " key_console=" + BoolText(IsKeyConsoleEnabled()) +
                 " pause_resume=" + BoolText(IsPauseResumePhrasesEnabled()) +
                 " quick_use_potions=" + BoolText(IsQuickUsePotionsEnabled()) +
                 " use_best_potion=" + BoolText(IsUseBestPotionEnabled()) +
                 " specify_hand=" + BoolText(IsSpecifyHandEnabled()) +
                 " quick_equip=" + BoolText(IsQuickEquipEnabled()));
    }

    static void QueueFavoritesSync()
    {
        if (!AnyFavoritesFeatureEnabled()) {
            return;
        }

        if (auto* tasks = SKSE::GetTaskInterface(); tasks) {
            tasks->AddTask([] {
                ScanAllFavorites(true);
                RefreshVoiceCommandState();
            });
        }
    }

    static void CompleteSaveReadySync()
    {
        SetGameLoaded(true);
        SendRuntimeConfigInternal();
        RegisterFavoritesWatcher();
        if (auto* tasks = SKSE::GetTaskInterface(); tasks) {
            tasks->AddTask([] {
                if (AnyFavoritesFeatureEnabled()) {
                    ScanAllFavorites(true);
                }
                RefreshVoiceCommandState();
                LogDebug("[LOAD] Save loaded");
            });
        } else {
            LogDebug("[LOAD] Save loaded");
        }
    }

    static bool IsPlayerCombatReady()
    {
        auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            return false;
        }

        auto actorState = player->AsActorState();
        if (!actorState) {
            return false;
        }

        return actorState->IsWeaponDrawn();
    }

    static bool IsShoutContextAllowed()
    {
        return IsPlayerInCombat() || IsPlayerCombatReady();
    }

    static RE::Actor* FindNPCInFront(float maxDist, float& outDist)
    {
        auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) return nullptr;

        auto playerPos = player->GetPosition();

        float yaw = player->GetAngleZ();
        RE::NiPoint3 lookDir;
        lookDir.x = std::sin(yaw);
        lookDir.y = std::cos(yaw);
        lookDir.z = 0.0f;

        RE::Actor* bestActor = nullptr;
        float bestDist = maxDist + 1.0f;

        auto processLists = RE::ProcessLists::GetSingleton();
        if (!processLists) return nullptr;

        for (auto& handle : processLists->highActorHandles) {
            auto actor = handle.get().get();
            if (!actor || actor == player) continue;
            if (actor->IsDead()) continue;
            if (actor->IsHostileToActor(player)) continue;

            auto actorPos = actor->GetPosition();
            RE::NiPoint3 toActor = actorPos - playerPos;
            float dist = toActor.Length();

            if (dist > maxDist || dist >= bestDist) continue;

            toActor.x /= dist;
            toActor.y /= dist;
            toActor.z = 0.0f;

            float dot = lookDir.x * toActor.x + lookDir.y * toActor.y;
            if (dot < kLookAtCosThreshold) continue;

            bestActor = actor;
            bestDist = dist;
        }

        outDist = bestDist;
        return bestActor;
    }

    static bool IsValidNPCTarget(RE::TESObjectREFR* ref, float& outDist)
    {
        if (!ref) return false;

        auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) return false;

        auto actor = ref->As<RE::Actor>();
        if (!actor) return false;

        if (actor->IsDead()) return false;

        auto playerPos = player->GetPosition();
        auto targetPos = ref->GetPosition();
        float dist = playerPos.GetDistance(targetPos);
        outDist = dist;

        if (dist > kFocusMaxDistCm) return false;

        return true;
    }

    static void ActivateTarget(RE::TESObjectREFR* target)
    {
        if (!target) return;

        auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) return;

        LogDebug("[ACTIVATE] firing on target: " + std::string(target->GetName() ? target->GetName() : "???"));
        target->ActivateRef(player, 0, nullptr, 0, false);
    }

    static void UpdateFocusDetection()
    {
        if (IsDialogueOpen()) {
            if (g_listenModeActive.load()) {
                g_listenModeActive.store(false);
                RefreshListenState();
            }
            return;
        }

        if (IsBlockingMenuOpen()) {
            if (g_listenModeActive.load()) {
                RefreshListenState();
            }
            ClearFocusListenState();
            return;
        }

        if (!IsVoiceOpenEnabled()) {
            if (g_listenModeActive.load()) {
                g_listenModeActive.store(false);
                RefreshListenState();
            }
            return;
        }

        auto ui = RE::UI::GetSingleton();
        if (ui && ui->GameIsPaused()) {
            return;
        }

        auto now = std::chrono::steady_clock::now();

        float dist = 0.0f;
        RE::Actor* currentTarget = FindNPCInFront(kFocusMaxDistCm, dist);

        bool hasFocus = (currentTarget != nullptr);

        if (hasFocus && !g_hadFocusLastPoll) {
            g_focusAcquiredTime = now;
            g_focusedActorHandle = currentTarget->GetHandle();
            LogDebug("[FOCUS] acquired: " + std::string(currentTarget->GetName() ? currentTarget->GetName() : "???") +
                    " dist=" + std::to_string(static_cast<int>(dist)));
        } else if (!hasFocus && g_hadFocusLastPoll) {
            g_focusLostTime = now;
            LogDebug("[FOCUS] lost");
        }

        g_hadFocusLastPoll = hasFocus;

        if (hasFocus) {
            auto focusDuration = std::chrono::duration_cast<std::chrono::milliseconds>(now - g_focusAcquiredTime).count();

            if (!g_listenModeActive.load() && focusDuration >= kFocusOnDelayMs) {
                g_listenModeActive.store(true);
                RefreshListenState();
            }
        } else {
            auto lostDuration = std::chrono::duration_cast<std::chrono::milliseconds>(now - g_focusLostTime).count();

            if (g_listenModeActive.load() && lostDuration >= kFocusGraceMs) {
                g_listenModeActive.store(false);
                RefreshListenState();
                g_focusedActorHandle.reset();
            }
        }
    }

    static void HandleOpenTrigger(const PipeResponse& resp)
    {
        auto target = g_focusedActorHandle.get().get();
        if (!target) {
            LogWarn("Dialogue command recognized=" + Quote(resp.trigText) +
                    " result=open status=FAIL reason=\"no valid focus target\"" +
                    " score=" + FormatScore(resp.score));
            return;
        }

        float dist = 0.0f;
        if (!IsValidNPCTarget(target, dist)) {
            LogWarn("Dialogue command recognized=" + Quote(resp.trigText) +
                    " result=open status=FAIL reason=\"target no longer valid dist=" +
                    std::to_string(static_cast<int>(dist)) + "\"" +
                    " score=" + FormatScore(resp.score));
            return;
        }

        g_listenModeActive.store(false);
        PipeClient::Get().SendConfigOpen(false);
        PipeClient::Get().SendListen(false);

        RE::ObjectRefHandle handleCopy = g_focusedActorHandle;
        const std::string targetName = target->GetName() ? target->GetName() : "???";
        LogInfo("Dialogue command recognized=" + Quote(resp.trigText) +
                " result=open status=OK target=" + Quote(targetName) +
                " score=" + FormatScore(resp.score));
        SKSE::GetTaskInterface()->AddTask([handleCopy]() {
            auto targetRef = handleCopy.get().get();
            if (targetRef) {
                ActivateTarget(targetRef);
                LogDebug("[ACTIVATE] fired via open trigger");
            }
        });
    }

    static void PollLoop()
    {
        LogDebug("[DVC_SERVER] poll thread started");

        bool hadAnyPipeConnection = false;
        std::uint64_t activeSaveGen = 0;
        bool saveReadyObserved = false;
        std::chrono::steady_clock::time_point saveFirstReady;

        while (g_running.load()) {
            UpdateFocusDetection();
            if (IsGameLoaded()) {
                SyncShoutContextState();
            }

            if (auto resp = PipeClient::Get().ConsumeLastResponse(); resp.has_value()) {
                if (resp->type == "DBG") {
                    std::string recognizedText;
                    if (TryExtractRecognitionText(resp->trigText, recognizedText)) {
                        g_lastRecognitionText = std::move(recognizedText);
                    }
                    if (IsPauseResumeDebugMessage(resp->trigText)) {
                        QueuePauseResumeSound();
                    }
                    DebugNotify(resp->trigText);
                } else if (resp->type == "TRIG") {
                    if (resp->trigKind == "open") {
                        HandleOpenTrigger(resp.value());
                    } else if (resp->trigKind == "shout") {
                        HandleVoiceTrigger(resp.value());
                    } else if (resp->trigKind == "power") {
                        HandlePowerTrigger(resp.value());
                    } else if (resp->trigKind == "weapon") {
                        HandleWeaponTrigger(resp.value());
                    } else if (resp->trigKind == "spell") {
                        HandleSpellTrigger(resp.value());
                    } else if (resp->trigKind == "potion") {
                        HandlePotionTrigger(resp.value());
                    } else if (resp->trigKind == "custom") {
                        HandleCustomCommandTrigger(resp.value());
                    }
                } else if (resp->type == "RES" && IsDialogueOpen()) {
                    LogDebug("[DVC_SERVER] recv index=" + std::to_string(resp->index) +
                        " score=" + std::to_string(resp->score));

                    if (resp->index == -2) {
                        LogInfo("Dialogue command result=close status=OK score=" + FormatScore(resp->score));
                        g_lastRecognitionText.clear();
                        SKSE::GetTaskInterface()->AddTask([]() {
                            auto ui = RE::UI::GetSingleton();
                            auto is = RE::InterfaceStrings::GetSingleton();
                            if (!ui || !is) return;

                            auto menu = ui->GetMenu(is->dialogueMenu);
                            if (!menu) return;

                            auto movie = menu->uiMovie.get();
                            if (!movie) return;

                            movie->Invoke("_level0.DialogueMenu_mc.StartHideMenu", nullptr, nullptr, 0);
                        });
                        continue;
                    }

                    if (resp->index < 0) {
                        g_lastRecognitionText.clear();
                    } else {
                        RequestSelectIndex_MainThread(resp->index, g_lastRecognitionText, resp->score);
                        g_lastRecognitionText.clear();
                    }
                }
            }

            if (auto ev = PipeClient::Get().ConsumeConnectionEvent(); ev.has_value()) {
                const bool connected = ev.value();
                if (connected) {
                    DebugNotify(hadAnyPipeConnection ? "Runtime restarted" : "Runtime connected");
                    hadAnyPipeConnection = true;
                } else {
                    DebugNotify("Runtime disconnected");
                }
            }

            if (auto ev = PipeClient::Get().ConsumeClientReadyEvent(); ev.has_value()) {
                if (ev.value() && IsGameLoaded()) {
                    ForceRuntimeSync();
                }
            }

            const std::uint64_t requestedGen = g_saveSyncRequestGen.load();
            if (g_saveSyncPending.load() && requestedGen != activeSaveGen) {
                activeSaveGen = requestedGen;
                saveFirstReady = {};
                saveReadyObserved = false;
                g_saveProbeHasResult.store(false);
                g_saveProbeInFlight.store(false);
            }

            if (g_saveSyncPending.load() && activeSaveGen != 0) {
                const auto now = std::chrono::steady_clock::now();
                RequestSaveReadyProbe(activeSaveGen);

                if (g_saveProbeHasResult.load() && g_saveProbeResultGen.load() == activeSaveGen) {
                    const bool ready = g_saveProbeReadyResult.load();
                    g_saveProbeHasResult.store(false);
                    if (ready && !saveReadyObserved) {
                        saveReadyObserved = true;
                        saveFirstReady = now;
                    } else if (!ready) {
                        saveReadyObserved = false;
                        saveFirstReady = {};
                    }
                }

                bool debouncedReady = false;
                if (saveReadyObserved) {
                    const auto readyMs = std::chrono::duration_cast<std::chrono::milliseconds>(now - saveFirstReady).count();
                    debouncedReady = readyMs >= kSaveReadyDebounceMs;
                }

                if (debouncedReady && PipeClient::Get().IsClientReady()) {
                    g_saveSyncPending.store(false);
                    CompleteSaveReadySync();
                }
            }

            if (IsDialogueOpen()) {
                LogOptionsIfChanged("POLL");
            }

            std::this_thread::sleep_for(std::chrono::milliseconds(kFocusPollMs));
        }
    }

    void StartPollThread()
    {
        g_running.store(true);
        g_pollThread = std::thread(PollLoop);
    }

    void BeginSaveLoading()
    {
        SetGameLoaded(false);
        g_saveSyncPending.store(false);
        g_saveSyncRequestGen.fetch_add(1);
    }

    void RequestSaveReadySync(const char* label)
    {
        (void)label;
        g_saveSyncRequestGen.fetch_add(1);
        g_saveSyncPending.store(true);
    }

    void SendRuntimeConfig()
    {
        SendRuntimeConfigInternal();
    }

    void ForceRuntimeSync()
    {
        if (!PipeClient::Get().IsClientReady()) {
            return;
        }
        if (!IsGameLoaded()) {
            return;
        }
        SendRuntimeConfigInternal();
        SyncShoutContextState();
        QueueFavoritesSync();
        RefreshListenState();
    }

    void RefreshVoiceCommandState()
    {
        RefreshListenState();
    }

    bool IsBlockingMenuOpen()
    {
        std::lock_guard lg(g_blockingMenuMutex);
        return !g_openBlockingMenus.empty();
    }

    class MenuGateWatcher : public RE::BSTEventSink<RE::MenuOpenCloseEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(
            const RE::MenuOpenCloseEvent* a_event,
            RE::BSTEventSource<RE::MenuOpenCloseEvent>*) override
        {
            if (!a_event) {
                return RE::BSEventNotifyControl::kContinue;
            }

            const std::string_view menuName = a_event->menuName.data();
            if (!IsBlockingMenuName(menuName)) {
                return RE::BSEventNotifyControl::kContinue;
            }

            bool becameBlocked = false;
            bool becameUnblocked = false;

            {
                std::lock_guard lg(g_blockingMenuMutex);
                const bool wasBlocked = !g_openBlockingMenus.empty();
                if (a_event->opening) {
                    g_openBlockingMenus.insert(std::string(menuName));
                } else {
                    g_openBlockingMenus.erase(std::string(menuName));
                }
                const bool isBlocked = !g_openBlockingMenus.empty();
                becameBlocked = !wasBlocked && isBlocked;
                becameUnblocked = wasBlocked && !isBlocked;
            }

            if (becameBlocked && !IsDialogueOpen()) {
                StopCaptureForBlockingMenu(menuName);
            } else if (becameUnblocked && !IsDialogueOpen()) {
                LogDebug("[MENU] blocking menu closed, resume capture");
                RefreshVoiceCommandState();
            }

            return RE::BSEventNotifyControl::kContinue;
        }
    };

    static MenuGateWatcher g_menuGateWatcher;

    void RegisterMenuGateWatcher()
    {
        if (auto ui = RE::UI::GetSingleton()) {
            ui->AddEventSink(&g_menuGateWatcher);
            LogDebug("[MENU] MenuGateWatcher registered");
        } else {
            LogLine("[MENU][WARN] UI singleton not available");
        }
    }

    void SyncShoutContextState()
    {
        if (!IsGameLoaded()) {
            return;
        }
        PipeClient::Get().SendPlayerDrawnState(IsPlayerCombatReady());
        PipeClient::Get().SendPlayerCombatState(IsPlayerInCombat());
        PipeClient::Get().SendBlockingMenuState(IsBlockingMenuOpen());
        PipeClient::Get().SendShoutContext(IsShoutContextAllowed());
    }

    void StopPollThread()
    {
        g_running.store(false);
        if (g_pollThread.joinable()) {
            g_pollThread.join();
        }
    }
}
