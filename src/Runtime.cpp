#include "PCH.h"

#include "Runtime.h"

#include "Common.h"
#include "Dialogue.h"
#include "FavoritesWatcher.h"
#include "Logging.h"
#include "Settings.h"
#include "ShoutsInternal.h"
#include "VoiceHandle.h"
#include "PipeClient.h"

#include <atomic>
#include <chrono>
#include <cmath>
#include <string>
#include <thread>

namespace DragonbornVoiceControl
{
    static constexpr int kFocusPollMs = 150;
    static constexpr int kFocusOnDelayMs = 250;
    static constexpr int kFocusGraceMs = 1500;
    static constexpr float kFocusMaxDistCm = 300.0f;
    static constexpr float kLookAtCosThreshold = 0.85f;

    static std::atomic_bool g_running{ true };
    static std::thread g_pollThread;

    static std::atomic_bool g_listenModeActive{ false };
    static RE::ObjectRefHandle g_focusedActorHandle;
    static std::chrono::steady_clock::time_point g_focusAcquiredTime;
    static std::chrono::steady_clock::time_point g_focusLostTime;
    static bool g_hadFocusLastPoll = false;

    static bool HasAnyVoiceCommandFeaturesEnabled()
    {
        return IsVoiceShoutsEnabled() || IsEnablePowersEnabled() || IsWeaponsEnabled() ||
               IsSpellsEnabled() || IsPotionsEnabled();
    }

    static bool IsPlayerInCombat()
    {
        auto player = RE::PlayerCharacter::GetSingleton();
        return player && player->IsInCombat();
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

        LogLine("[ACTIVATE] firing on target: " + std::string(target->GetName() ? target->GetName() : "???"));
        target->ActivateRef(player, 0, nullptr, 0, false);
    }

    static void UpdateFocusDetection()
    {
        if (IsDialogueOpen()) {
            if (g_listenModeActive.load()) {
                g_listenModeActive.store(false);
                PipeClient::Get().SendListen(false);
                RefreshVoiceCommandState();
            }
            return;
        }

        if (!IsVoiceOpenEnabled()) {
            if (g_listenModeActive.load()) {
                g_listenModeActive.store(false);
                PipeClient::Get().SendListen(false);
                RefreshVoiceCommandState();
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
            LogLine("[FOCUS] acquired: " + std::string(currentTarget->GetName() ? currentTarget->GetName() : "???") +
                    " dist=" + std::to_string(static_cast<int>(dist)));
        } else if (!hasFocus && g_hadFocusLastPoll) {
            g_focusLostTime = now;
            LogLine("[FOCUS] lost");
        }

        g_hadFocusLastPoll = hasFocus;

        if (hasFocus) {
            auto focusDuration = std::chrono::duration_cast<std::chrono::milliseconds>(now - g_focusAcquiredTime).count();

            if (!g_listenModeActive.load() && focusDuration >= kFocusOnDelayMs) {
                g_listenModeActive.store(true);
                PipeClient::Get().SendListen(true);
                RefreshVoiceCommandState();
            }
        } else {
            auto lostDuration = std::chrono::duration_cast<std::chrono::milliseconds>(now - g_focusLostTime).count();

            if (g_listenModeActive.load() && lostDuration >= kFocusGraceMs) {
                g_listenModeActive.store(false);
                PipeClient::Get().SendListen(false);
                RefreshVoiceCommandState();
                g_focusedActorHandle.reset();
            }
        }
    }

    static void HandleOpenTrigger(const PipeResponse& resp)
    {
        LogLine("[TRIG] open received: score=" + std::to_string(resp.score) + " text=\"" + resp.trigText + "\"");

        if (!IsVoiceOpenEnabled()) {
            LogLine("[TRIG] EnableVoiceOpen=0, ignoring open trigger");
            return;
        }

        auto target = g_focusedActorHandle.get().get();
        if (!target) {
            LogLine("[TRIG] no valid focus target, ignoring");
            return;
        }

        float dist = 0.0f;
        if (!IsValidNPCTarget(target, dist)) {
            LogLine("[TRIG] target no longer valid (dist=" + std::to_string(static_cast<int>(dist)) + ")");
            return;
        }

        g_listenModeActive.store(false);
        PipeClient::Get().SendListen(false);
        PipeClient::Get().SendListenCommands(false);

        RE::ObjectRefHandle handleCopy = g_focusedActorHandle;
        SKSE::GetTaskInterface()->AddTask([handleCopy]() {
            auto targetRef = handleCopy.get().get();
            if (targetRef) {
                ActivateTarget(targetRef);
                LogLine("[ACTIVATE] fired via open trigger");
            }
        });
    }

    static void PollLoop()
    {
        LogLine("[DVC_SERVER] poll thread started");

        bool hadAnyPipeConnection = false;

        while (g_running.load()) {
            UpdateFocusDetection();
            SyncShoutContextState();

            if (auto resp = PipeClient::Get().ConsumeLastResponse(); resp.has_value()) {
                if (resp->type == "DBG") {
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
                    }
                } else if (resp->type == "RES" && IsDialogueOpen()) {
                    if (!IsDialogueSelectEnabled()) {
                        LogLine("[VOICE] dialogue select disabled, ignoring RES");
                        continue;
                    }

                        LogLine("[DVC_SERVER] recv index=" + std::to_string(resp->index) +
                            " score=" + std::to_string(resp->score));

                    if (resp->index == -2) {
                        LogLine("[VOICE][CLOSE] request");

                        if (!IsVoiceCloseEnabled()) {
                            LogLine("[VOICE][CLOSE] ignored: EnableVoiceClose=0");
                            continue;
                        }

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
                        LogLine("[VOICE] no match / ignore");
                    } else {
                        LogLine("[VOICE][SELECT] index0=" + std::to_string(resp->index) +
                                " score=" + std::to_string(resp->score));

                        RequestSelectIndex_MainThread(resp->index);
                    }
                }
            }

            if (auto ev = PipeClient::Get().ConsumeConnectionEvent(); ev.has_value()) {
                const bool connected = ev.value();
                if (connected) {
                    DebugNotify(hadAnyPipeConnection ? "Runtime restarted" : "Runtime connected");
                    hadAnyPipeConnection = true;

                    if (IsGameLoaded()) {
                        // Re-sync server runtime state after reconnect/restart.
                        // Server process may lose in-memory CFG and shout grammar restrictions.
                        PipeClient::Get().SendConfigOpen(IsVoiceOpenEnabled());
                        PipeClient::Get().SendConfigClose(IsVoiceCloseEnabled());
                        PipeClient::Get().SendConfigDialogueSelect(IsDialogueSelectEnabled());
                        PipeClient::Get().SendConfigShouts(IsVoiceShoutsEnabled());
                        PipeClient::Get().SendConfigPowers(IsEnablePowersEnabled());
                        PipeClient::Get().SendConfigDebug(IsDebugEnabled());
                        PipeClient::Get().SendConfigSaveWav(IsSaveWavCapturesEnabled());
                        PipeClient::Get().SendConfigWeapons(IsWeaponsEnabled());
                        PipeClient::Get().SendConfigSpells(IsSpellsEnabled());
                        PipeClient::Get().SendConfigPotions(IsPotionsEnabled());
                        SyncShoutContextState();
                    }

                    // Only rescan favorites if the game is actually loaded.
                    // Until PostLoadGame/NewGame, player data isn't ready, so
                    // ScanAllFavorites would return empty arrays and the server
                    // would receive grammar with 0 entries while LISTEN|SHOUTS=ON.
                    // PostLoadGame handler in plugin.cpp takes care of the initial sync.
                    if (detail::g_gameLoaded.load()) {
                        SKSE::GetTaskInterface()->AddTask([] {
                            ScanAllFavorites(true);
                            RefreshVoiceCommandState();
                        });
                    }
                } else {
                    DebugNotify("Runtime disconnected");
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

    void RefreshVoiceCommandState()
    {
        const bool enableCommands = HasAnyVoiceCommandFeaturesEnabled() && !IsDialogueOpen();
        PipeClient::Get().SendListenCommands(enableCommands);
        SyncShoutContextState();
    }

    void SyncShoutContextState()
    {
        PipeClient::Get().SendPlayerDrawnState(IsPlayerCombatReady());
        PipeClient::Get().SendPlayerCombatState(IsPlayerInCombat());
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
