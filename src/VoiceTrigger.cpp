#include "PCH.h"
#include "VoiceTrigger.h"

#include "Common.h"
#include "Logging.h"
#include "Settings.h"
#include "ShoutsInternal.h"
#include "VoiceHandle.h"

#include <RE/F/FunctionArguments.h>

#include <charconv>
#include <chrono>
#include <string>
#include <thread>

namespace DragonbornVoiceControl
{
    // ─────────────── helpers ───────────────

    static inline void ShoutLog(const std::string& msg)
    {
        LogLine("[SHOUT][TRIGGER] " + msg);
    }

    static inline void ShoutDbg(const std::string& msg)
    {
        if (IsDebugEnabled()) {
            LogLine("[SHOUT][TRIGGER][DBG] " + msg);
        }
    }

    static RE::FormID ParseHexFormID(const std::string& hex)
    {
        std::string_view sv(hex);
        if (sv.starts_with("0x") || sv.starts_with("0X")) {
            sv.remove_prefix(2);
        }
        RE::FormID result = 0;
        auto [ptr, ec] = std::from_chars(sv.data(), sv.data() + sv.size(), result, 16);
        if (ec != std::errc{}) {
            ShoutLog("ERROR: failed to parse FormID hex string: \"" + hex + "\"");
            return 0;
        }
        return result;
    }

    /// Clamp power to [1..3].
    static int ClampPower(int power)
    {
        if (power < 1) return 1;
        if (power > 3) return 3;
        return power;
    }

    // ─────────────── core validation ───────────────

    struct ShoutContext
    {
        RE::FormID          formID   = 0;
        int                 power    = 1;      // 1-based
        RE::TESShout*       shout    = nullptr;
        RE::SpellItem*      spell    = nullptr;
    };

    struct PowerContext
    {
        RE::FormID     formID = 0;
        RE::SpellItem* power  = nullptr;
    };

    static RE::FormID ComposeRuntimeFormID(const std::string& plugin, RE::FormID baseId)
    {
        if (plugin.empty()) {
            return 0;
        }

        auto* dataHandler = RE::TESDataHandler::GetSingleton();
        if (!dataHandler) {
            return 0;
        }

        const auto pluginName = std::string_view(plugin);
        if (auto idx = dataHandler->GetLoadedModIndex(pluginName); idx.has_value()) {
            return (static_cast<RE::FormID>(idx.value()) << 24) | (baseId & 0x00FFFFFF);
        }
        if (auto idx = dataHandler->GetLoadedLightModIndex(pluginName); idx.has_value()) {
            const auto lightId = static_cast<RE::FormID>(idx.value()) & 0x00000FFF;
            return 0xFE000000 | (lightId << 12) | (baseId & 0x00000FFF);
        }

        return 0;
    }

    /// Validate inputs and fill ShoutContext.  Returns true if ready to fire.
    /// Only checks formID parsing, shout lookup, and variation validity.
    /// The game itself handles cooldown, menu guards, etc. via simulated key.
    static bool ValidateShoutContext(const PipeResponse& resp, ShoutContext& ctx)
    {
        // 1.  Parse base FormID + plugin
        const RE::FormID baseId = ParseHexFormID(resp.shoutFormID);
        if (baseId == 0) {
            ShoutLog("FAIL: base FormID is 0 (parse error)");
            return false;
        }

        ctx.formID = ComposeRuntimeFormID(resp.shoutPlugin, baseId);
        if (ctx.formID == 0) {
            ShoutLog("FAIL: runtime FormID is 0 (plugin not found?)");
            return false;
        }
        ShoutDbg("FormID resolved: 0x" + detail::FormIDToHex(ctx.formID));

        // 2.  Power
        ctx.power = ClampPower(resp.shoutPower);
        ShoutDbg("Power (clamped): " + std::to_string(ctx.power));

        // 3.  Lookup TESShout
        ctx.shout = RE::TESForm::LookupByID<RE::TESShout>(ctx.formID);
        if (!ctx.shout) {
            ShoutLog("FAIL: TESShout not found for FormID 0x" +
                     detail::FormIDToHex(ctx.formID));
            return false;
        }
        const char* name = ctx.shout->GetFullName();
        ShoutDbg("TESShout lookup: OK — \"" + std::string(name ? name : "???") + "\"");

        // 4.  Variation validity
        const int vi = ctx.power - 1;
        const auto& var = ctx.shout->variations[vi];
        ctx.spell = var.spell;

        if (!ctx.spell) {
            ShoutLog("FAIL: variations[" + std::to_string(vi) +
                     "].spell is nullptr for shout 0x" +
                     detail::FormIDToHex(ctx.formID));
            return false;
        }
        ShoutDbg("Variation[" + std::to_string(vi) + "] spell OK");

        return true;
    }

    static bool ValidatePowerContext(const PipeResponse& resp, PowerContext& ctx)
    {
        ctx.formID = ParseHexFormID(resp.powerFormID);
        if (ctx.formID == 0) {
            ShoutLog("FAIL: Power FormID is 0 (parse error)");
            return false;
        }

        ctx.power = RE::TESForm::LookupByID<RE::SpellItem>(ctx.formID);
        if (!ctx.power) {
            ShoutLog("FAIL: SpellItem not found for power FormID 0x" + detail::FormIDToHex(ctx.formID));
            return false;
        }

        const auto type = ctx.power->GetSpellType();
        if (type != RE::MagicSystem::SpellType::kPower && type != RE::MagicSystem::SpellType::kLesserPower) {
            ShoutLog("FAIL: SpellItem is not a power FormID 0x" + detail::FormIDToHex(ctx.formID));
            return false;
        }

        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            ShoutLog("FAIL: PlayerCharacter nullptr (validation)");
            return false;
        }
        if (!player->HasSpell(ctx.power)) {
            ShoutLog("FAIL: Player does not know power FormID 0x" + detail::FormIDToHex(ctx.formID));
            return false;
        }

        return true;
    }

    // ═══════════════════════════════════════════════════════════
    //  Player-specific voice-line muting.
    //  Stops any valid player sound handles during a short window
    //  after the shout fires.  Does NOT affect NPC audio.
    // ═══════════════════════════════════════════════════════════

    static bool IsMuteWindowActive()
    {
        return IsMuteShoutVoiceLineEnabled() && detail::g_muteShoutVoiceWindow.load();
    }

    static int StopPlayerSounds(RE::PlayerCharacter* player,
                                const char* tag)
    {
        if (!player) return 0;

        RE::AIProcess* proc = player->GetActorRuntimeData().currentProcess;
        if (!proc || !proc->high) return 0;

        int stopped = 0;
        for (int i = 0; i < 2; ++i) {
            auto& h = proc->high->soundHandles[i];
            if (!h.IsValid()) continue;

            if (h.Stop()) {
                ++stopped;
                ShoutDbg(std::string("[MUTE]") +
                         (tag ? std::string("[") + tag + "]" : "") +
                         " stopped player sound idx=" + std::to_string(i) +
                         " soundID=" + std::to_string(h.soundID));
            }
        }
        return stopped;
    }

    static void MuteVoiceLine(RE::PlayerCharacter* player)
    {
        // Open mute window
        detail::g_muteShoutVoiceWindow.store(true);
        const std::uint64_t gen = detail::g_muteShoutVoiceWindowGen.fetch_add(1) + 1;

        // Immediately try to stop any sounds that already appeared
        StopPlayerSounds(player, "immediate");

        // Poll on a short interval to avoid missing word segments.
        // Use a burst window with tighter timing at the start.
        std::thread([gen]() {
            constexpr int kMuteWindowMs = 1800;
            constexpr int kBurstWindowMs = 500;
            constexpr int kBurstIntervalMs = 15;
            constexpr int kPollIntervalMs = 30;

            int elapsedMs = 0;
            while (elapsedMs < kMuteWindowMs) {
                const int interval = (elapsedMs < kBurstWindowMs) ? kBurstIntervalMs : kPollIntervalMs;
                std::this_thread::sleep_for(std::chrono::milliseconds(interval));
                elapsedMs += interval;

                if (detail::g_muteShoutVoiceWindowGen.load() != gen) {
                    return;
                }
                if (!IsMuteWindowActive()) {
                    return;
                }

                if (auto* t = SKSE::GetTaskInterface(); t) {
                    t->AddTask([]() {
                        if (!IsMuteWindowActive()) return;
                        if (auto* p = RE::PlayerCharacter::GetSingleton()) {
                            StopPlayerSounds(p, "poll");
                        }
                    });
                }
            }

            if (detail::g_muteShoutVoiceWindowGen.load() == gen) {
                detail::g_muteShoutVoiceWindow.store(false);
            }
        }).detach();

        ShoutLog("[MUTE] player voice-line mute window opened (~1800ms)");
    }

    // ═══════════════════════════════════════════════════════════
    //  Shout trigger:  EquipShout + small delay + Papyrus SimulateShoutKey
    // ═══════════════════════════════════════════════════════════

    static void ExecuteVoiceTrigger(const ShoutContext& ctx)
    {
        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            ShoutLog("FAIL: PlayerCharacter nullptr (game thread)");
            return;
        }

        const bool muteVoice = IsMuteShoutVoiceLineEnabled();

        // 1.  Equip shout
        auto* eqMgr = RE::ActorEquipManager::GetSingleton();
        if (!eqMgr) {
            ShoutLog("FAIL: ActorEquipManager nullptr");
            return;
        }
        eqMgr->EquipShout(player, ctx.shout);
        ShoutDbg("EquipShout: OK");

        // 1b. Set selectedPower so the HUD updates immediately.
        {
            auto& rtData = player->GetActorRuntimeData();
            rtData.selectedPower = ctx.shout;
            rtData.selectedSpells[RE::Actor::SlotTypes::kPowerOrShout] = ctx.spell;
            ShoutDbg("selectedPower & selectedSpells[voice] set");
        }

        // 2.  Small delay so the engine registers the equipped shout
        //     prior to simulating the key press.
        constexpr int kEquipDelayMs = 100;
        std::thread([ctx, muteVoice]() {
            std::this_thread::sleep_for(std::chrono::milliseconds(kEquipDelayMs));

            SKSE::GetTaskInterface()->AddTask([ctx, muteVoice]() {
                // 3. Call Papyrus: DragonbornVoiceControlShout.SimulateShoutKey(power)
                auto* vm = RE::BSScript::Internal::VirtualMachine::GetSingleton();
                if (!vm) {
                    ShoutLog("FAIL: Papyrus VirtualMachine singleton nullptr");
                    return;
                }

                auto args = RE::MakeFunctionArguments(static_cast<std::int32_t>(ctx.power));
                RE::BSTSmartPointer<RE::BSScript::IStackCallbackFunctor> callback;

                bool dispatched = vm->DispatchStaticCall(
                    "DragonbornVoiceControlShout",
                    "SimulateShoutKey",
                    args,
                    callback);

                if (dispatched) {
                    ShoutLog("Papyrus SimulateShoutKey dispatched, power=" +
                             std::to_string(ctx.power) +
                             " \"" + (ctx.shout->GetFullName()
                                      ? ctx.shout->GetFullName() : "???") + "\"");
                } else {
                    ShoutLog("FAIL: DispatchStaticCall returned false "
                             "(script not loaded? .pex missing?)");
                }

                // Mute voice line if configured
                if (muteVoice) {
                    if (auto* p = RE::PlayerCharacter::GetSingleton()) {
                        MuteVoiceLine(p);
                    }
                }
            });
        }).detach();
    }

    static void ExecutePowerTrigger(const PowerContext& ctx)
    {
        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            ShoutLog("FAIL: PlayerCharacter nullptr (game thread)");
            return;
        }

        auto* eqMgr = RE::ActorEquipManager::GetSingleton();
        if (!eqMgr) {
            ShoutLog("FAIL: ActorEquipManager nullptr");
            return;
        }

        eqMgr->EquipSpell(player, ctx.power, nullptr);

        {
            auto& rtData = player->GetActorRuntimeData();
            rtData.selectedPower = ctx.power;
            rtData.selectedSpells[RE::Actor::SlotTypes::kPowerOrShout] = ctx.power;
        }

        auto* vm = RE::BSScript::Internal::VirtualMachine::GetSingleton();
        if (!vm) {
            ShoutLog("FAIL: Papyrus VirtualMachine singleton nullptr");
            return;
        }

        auto args = RE::MakeFunctionArguments(static_cast<std::int32_t>(1));
        RE::BSTSmartPointer<RE::BSScript::IStackCallbackFunctor> callback;

        bool dispatched = vm->DispatchStaticCall(
            "DragonbornVoiceControlShout",
            "SimulateShoutKey",
            args,
            callback);

        if (dispatched) {
            ShoutLog("Power activated via SimulateShoutKey (tap)");
        } else {
            ShoutLog("FAIL: DispatchStaticCall returned false for power");
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  Public entry point (called from poll thread)
    // ═══════════════════════════════════════════════════════════

    void TriggerShout(const PipeResponse& resp)
    {
        ShoutLog("trigger=PapyrusInput, mute=PlayerSoundHandles");

        // Validate on caller thread (poll thread) — fast checks only
        ShoutContext ctx{};
        if (!ValidateShoutContext(resp, ctx)) {
            ShoutLog("TriggerShout aborted (validation failed)");
            return;
        }

        // Capture context by value for the game-thread lambda
        ShoutContext captured = ctx;

        SKSE::GetTaskInterface()->AddTask([captured]() {
            ShoutDbg(">>> game-thread task entered");
            ExecuteVoiceTrigger(captured);
            ShoutDbg("<<< game-thread task exited");
        });

        ShoutLog("queued shout task on game thread: formID=0x" +
                 detail::FormIDToHex(ctx.formID) +
                 " power=" + std::to_string(ctx.power) +
                 " text=\"" + resp.trigText + "\"");
    }

    void TriggerPower(const PipeResponse& resp)
    {
        ShoutLog("trigger=Power");

        PowerContext ctx{};
        if (!ValidatePowerContext(resp, ctx)) {
            ShoutLog("TriggerPower aborted (validation failed)");
            return;
        }

        PowerContext captured = ctx;
        if (auto* t = SKSE::GetTaskInterface(); t) {
            t->AddTask([captured]() {
                ExecutePowerTrigger(captured);
            });
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  Weapon trigger:  Equip weapon to right hand
    // ═══════════════════════════════════════════════════════════

    void TriggerWeapon(const PipeResponse& resp)
    {
        ShoutLog("trigger=Weapon");

        RE::FormID formID = ParseHexFormID(resp.itemFormID);
        if (formID == 0) {
            ShoutLog("FAIL: Weapon FormID is 0 (parse error)");
            return;
        }

        if (auto* t = SKSE::GetTaskInterface(); t) {
            t->AddTask([formID]() {
                auto* player = RE::PlayerCharacter::GetSingleton();
                if (!player) {
                    ShoutLog("FAIL: PlayerCharacter nullptr");
                    return;
                }

                auto* weap = RE::TESForm::LookupByID<RE::TESObjectWEAP>(formID);
                if (!weap) {
                    ShoutLog("FAIL: TESObjectWEAP not found for FormID 0x" + detail::FormIDToHex(formID));
                    return;
                }

                // Check player has this weapon in inventory
                auto inv = player->GetInventory();
                bool hasItem = false;
                for (auto& [obj, data] : inv) {
                    if (obj && obj->GetFormID() == formID && data.first > 0) {
                        hasItem = true;
                        break;
                    }
                }
                if (!hasItem) {
                    ShoutLog("FAIL: Player does not have weapon 0x" + detail::FormIDToHex(formID));
                    return;
                }

                auto* eqMgr = RE::ActorEquipManager::GetSingleton();
                if (!eqMgr) {
                    ShoutLog("FAIL: ActorEquipManager nullptr");
                    return;
                }

                // Equip to right hand
                auto* rightSlot = RE::BGSDefaultObjectManager::GetSingleton()->GetObject<RE::BGSEquipSlot>(
                    RE::DEFAULT_OBJECT::kRightHandEquip);

                eqMgr->EquipObject(player, weap, nullptr, 1, rightSlot);

                const char* name = weap->GetFullName();
                ShoutLog("Weapon equipped (right hand): \"" +
                         std::string(name ? name : "???") + "\"");
            });
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  Spell trigger:  Equip spell to right hand
    // ═══════════════════════════════════════════════════════════

    void TriggerSpell(const PipeResponse& resp)
    {
        ShoutLog("trigger=Spell");

        RE::FormID formID = ParseHexFormID(resp.itemFormID);
        if (formID == 0) {
            ShoutLog("FAIL: Spell FormID is 0 (parse error)");
            return;
        }

        if (auto* t = SKSE::GetTaskInterface(); t) {
            t->AddTask([formID]() {
                auto* player = RE::PlayerCharacter::GetSingleton();
                if (!player) {
                    ShoutLog("FAIL: PlayerCharacter nullptr");
                    return;
                }

                auto* spell = RE::TESForm::LookupByID<RE::SpellItem>(formID);
                if (!spell) {
                    ShoutLog("FAIL: SpellItem not found for FormID 0x" + detail::FormIDToHex(formID));
                    return;
                }

                if (!player->HasSpell(spell)) {
                    ShoutLog("FAIL: Player does not know spell 0x" + detail::FormIDToHex(formID));
                    return;
                }

                auto* eqMgr = RE::ActorEquipManager::GetSingleton();
                if (!eqMgr) {
                    ShoutLog("FAIL: ActorEquipManager nullptr");
                    return;
                }

                // Equip to right hand
                auto* rightSlot = RE::BGSDefaultObjectManager::GetSingleton()->GetObject<RE::BGSEquipSlot>(
                    RE::DEFAULT_OBJECT::kRightHandEquip);

                eqMgr->EquipSpell(player, spell, rightSlot);

                const char* name = spell->GetFullName();
                ShoutLog("Spell equipped (right hand): \"" +
                         std::string(name ? name : "???") + "\"");
            });
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  Potion trigger:  Use a potion from inventory
    // ═══════════════════════════════════════════════════════════

    void TriggerPotion(const PipeResponse& resp)
    {
        ShoutLog("trigger=Potion");

        RE::FormID formID = ParseHexFormID(resp.itemFormID);
        if (formID == 0) {
            ShoutLog("FAIL: Potion FormID is 0 (parse error)");
            return;
        }

        if (auto* t = SKSE::GetTaskInterface(); t) {
            t->AddTask([formID]() {
                auto* player = RE::PlayerCharacter::GetSingleton();
                if (!player) {
                    ShoutLog("FAIL: PlayerCharacter nullptr");
                    return;
                }

                auto* alch = RE::TESForm::LookupByID<RE::AlchemyItem>(formID);
                if (!alch) {
                    ShoutLog("FAIL: AlchemyItem not found for FormID 0x" + detail::FormIDToHex(formID));
                    return;
                }

                // Check player has this potion in inventory
                auto inv = player->GetInventory();
                bool hasItem = false;
                for (auto& [obj, data] : inv) {
                    if (obj && obj->GetFormID() == formID && data.first > 0) {
                        hasItem = true;
                        break;
                    }
                }
                if (!hasItem) {
                    ShoutLog("FAIL: Player does not have potion 0x" + detail::FormIDToHex(formID));
                    return;
                }

                auto* eqMgr = RE::ActorEquipManager::GetSingleton();
                if (!eqMgr) {
                    ShoutLog("FAIL: ActorEquipManager nullptr");
                    return;
                }

                // EquipObject with a potion triggers consumption
                eqMgr->EquipObject(player, alch);

                const char* name = alch->GetFullName();
                ShoutLog("Potion used: \"" +
                         std::string(name ? name : "???") + "\"");
            });
        }
    }

} // namespace DragonbornVoiceControl