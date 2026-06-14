#include "PCH.h"
#include "VoiceTrigger.h"

#include "Common.h"
#include "Logging.h"
#include "Settings.h"
#include "ShoutsInternal.h"
#include "VoiceHandle.h"

#define WIN32_LEAN_AND_MEAN
#include <Windows.h>

#include <RE/F/FunctionArguments.h>

#include <algorithm>
#include <charconv>
#include <chrono>
#include <cctype>
#include <future>
#include <iomanip>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <vector>

namespace DragonbornVoiceControl
{
    // ─────────────── helpers ───────────────

    static inline void ShoutLog(const std::string& msg)
    {
        LogLine("[TRIGGER] " + msg);
    }

    static inline void ShoutDbg(const std::string& msg)
    {
        if (IsDebugEnabled()) {
            LogLine("[TRIGGER][DBG] " + msg);
        }
    }

    static std::string FormatScore(float score)
    {
        std::ostringstream out;
        out << std::fixed << std::setprecision(3) << score;
        return out.str();
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

    static std::string VoiceCommandBase(const std::string& text,
                                        const std::string& result,
                                        const std::string& kind,
                                        const std::string& formID)
    {
        return "Voice command recognized=" + Quote(text) +
               " result=" + result +
               " kind=" + kind +
               " formid=" + formID;
    }

    static void LogVoiceCommandInfo(const std::string& kind,
                                    const std::string& text,
                                    float score,
                                    const std::string& formID,
                                    const std::string& result,
                                    const std::string& name,
                                    const std::string& hand = "")
    {
        std::string line = VoiceCommandBase(text, result, kind, formID);
        if (!hand.empty()) {
            line += " hand=" + hand;
        }
        LogInfo(line +
                " name=" + Quote(name) +
                " score=" + FormatScore(score));
    }

    static void LogVoiceCommandWarn(const std::string& kind,
                                    const std::string& text,
                                    float score,
                                    const std::string& formID,
                                    const std::string& reason)
    {
        LogWarn(VoiceCommandBase(text, "failed", kind, formID) +
                " reason=" + Quote(reason) +
                " score=" + FormatScore(score));
    }

    static const char* NormalizeItemHand(const std::string& hand)
    {
        if (hand == "both") {
            return "both";
        }
        return hand == "left" ? "left" : "right";
    }

    static bool IsBothItemHands(const std::string& hand)
    {
        return std::string_view(NormalizeItemHand(hand)) == "both";
    }

    static bool IsLeftItemHand(const std::string& hand)
    {
        return std::string_view(NormalizeItemHand(hand)) == "left";
    }

    static RE::BGSEquipSlot* GetHandEquipSlot(const std::string& hand)
    {
        const bool left = IsLeftItemHand(hand);
        constexpr RE::FormID kRightHandEquipSlot = 0x00013F42;
        constexpr RE::FormID kLeftHandEquipSlot = 0x00013F43;
        return RE::TESForm::LookupByID<RE::BGSEquipSlot>(left ? kLeftHandEquipSlot : kRightHandEquipSlot);
    }

    static int GetInventoryItemCount(RE::PlayerCharacter* player, RE::FormID formID)
    {
        if (!player || formID == 0) {
            return 0;
        }

        int count = 0;
        auto inv = player->GetInventory();
        for (auto& [obj, data] : inv) {
            if (obj && obj->GetFormID() == formID && data.first > 0) {
                count += data.first;
            }
        }
        return count;
    }

    static int CountEquippedObject(RE::PlayerCharacter* player, RE::FormID formID)
    {
        if (!player || formID == 0) {
            return 0;
        }

        int count = 0;
        if (auto* right = player->GetEquippedObject(false); right && right->GetFormID() == formID) {
            ++count;
        }
        if (auto* left = player->GetEquippedObject(true); left && left->GetFormID() == formID) {
            ++count;
        }
        return count;
    }

    static bool IsObjectEquippedInRequestedHand(RE::PlayerCharacter* player, RE::FormID formID, const std::string& hand)
    {
        if (!player || formID == 0) {
            return false;
        }

        const bool leftHand = IsLeftItemHand(hand);
        auto* equipped = player->GetEquippedObject(leftHand);
        return equipped && equipped->GetFormID() == formID;
    }

    static bool IsObjectEquippedInOppositeHand(RE::PlayerCharacter* player, RE::FormID formID, const std::string& hand)
    {
        if (!player || formID == 0) {
            return false;
        }

        auto* equipped = player->GetEquippedObject(!IsLeftItemHand(hand));
        return equipped && equipped->GetFormID() == formID;
    }

    static void LogShoutCommandInfo(const PipeResponse& resp,
                                    RE::FormID formID,
                                    int power,
                                    const std::string& result,
                                    const std::string& name)
    {
        LogInfo(VoiceCommandBase(resp.trigText, result, "shout", "0x" + detail::FormIDToHex(formID)) +
                " power=" + std::to_string(power) +
                " name=" + Quote(name) +
                " score=" + FormatScore(resp.score));
    }

    static void LogShoutCommandWarn(const PipeResponse& resp,
                                    const std::string& formID,
                                    const std::string& reason)
    {
        LogWarn(VoiceCommandBase(resp.trigText, "failed", "shout", formID) +
                " reason=" + Quote(reason) +
                " power=" + std::to_string(resp.shoutPower) +
                " score=" + FormatScore(resp.score));
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
            return 0;
        }
        return result;
    }

    static std::string ToLowerCopy(std::string value)
    {
        std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
            return static_cast<char>(std::tolower(ch));
        });
        return value;
    }

    static std::vector<std::string> SplitParams(const std::string& command)
    {
        std::istringstream in(command);
        std::vector<std::string> out;
        std::string part;
        while (in >> part) {
            out.push_back(part);
        }
        return out;
    }

    static std::string TrimCopy(std::string value)
    {
        while (!value.empty() && std::isspace(static_cast<unsigned char>(value.front()))) {
            value.erase(value.begin());
        }
        while (!value.empty() && std::isspace(static_cast<unsigned char>(value.back()))) {
            value.pop_back();
        }
        return value;
    }

    static std::uint32_t ParseUInt(const std::string& value)
    {
        if (value.size() > 2 && value[0] == '0' && (value[1] == 'x' || value[1] == 'X')) {
            std::uint32_t out = 0;
            auto sv = std::string_view(value).substr(2);
            auto [ptr, ec] = std::from_chars(sv.data(), sv.data() + sv.size(), out, 16);
            return ec == std::errc{} ? out : 0;
        }

        std::uint32_t out = 0;
        auto sv = std::string_view(value);
        auto [ptr, ec] = std::from_chars(sv.data(), sv.data() + sv.size(), out, 10);
        return ec == std::errc{} ? out : 0;
    }

    static std::uint32_t GetKeyScanCode(std::string key)
    {
        static const std::unordered_map<std::string, std::uint32_t> keyMap = {
            {"escape", 1}, {"esc", 1}, {"1", 2}, {"2", 3}, {"3", 4}, {"4", 5}, {"5", 6},
            {"6", 7}, {"7", 8}, {"8", 9}, {"9", 10}, {"0", 11}, {"-", 12}, {"minus", 12},
            {"=", 13}, {"equal", 13}, {"equals", 13}, {"backspace", 14}, {"tab", 15},
            {"q", 16}, {"w", 17}, {"e", 18}, {"r", 19}, {"t", 20}, {"y", 21}, {"u", 22},
            {"i", 23}, {"o", 24}, {"p", 25}, {"[", 26}, {"leftbracket", 26}, {"]", 27},
            {"rightbracket", 27}, {"enter", 28}, {"leftcontrol", 29}, {"leftctrl", 29},
            {"lctrl", 29}, {"ctrl", 29}, {"control", 29}, {"a", 30}, {"s", 31}, {"d", 32},
            {"f", 33}, {"g", 34}, {"h", 35}, {"j", 36}, {"k", 37}, {"l", 38}, {";", 39},
            {"semicolon", 39}, {"'", 40}, {"apostrophe", 40}, {"`", 41}, {"~", 41},
            {"backquote", 41}, {"console", 41}, {"leftshift", 42}, {"lshift", 42},
            {"shift", 42}, {"\\", 43}, {"backslash", 43}, {"z", 44}, {"x", 45}, {"c", 46},
            {"v", 47}, {"b", 48}, {"n", 49}, {"m", 50}, {",", 51}, {"comma", 51},
            {".", 52}, {"period", 52}, {"/", 53}, {"slash", 53}, {"rightshift", 54},
            {"rshift", 54}, {"num*", 55}, {"numstar", 55}, {"leftalt", 56}, {"lalt", 56},
            {"alt", 56}, {"spacebar", 57}, {"space", 57}, {"blank", 57}, {"capslock", 58},
            {"caps", 58}, {"f1", 59}, {"f2", 60}, {"f3", 61}, {"f4", 62}, {"f5", 63},
            {"f6", 64}, {"f7", 65}, {"f8", 66}, {"f9", 67}, {"f10", 68}, {"numlock", 69},
            {"scrolllock", 70}, {"num7", 71}, {"num8", 72}, {"num9", 73}, {"num-", 74},
            {"num4", 75}, {"num5", 76}, {"num6", 77}, {"num+", 78}, {"num1", 79},
            {"num2", 80}, {"num3", 81}, {"num0", 82}, {"num.", 83}, {"f11", 87},
            {"f12", 88}, {"numenter", 156}, {"rightcontrol", 157}, {"rightctrl", 157},
            {"rctrl", 157}, {"num/", 181}, {"sysrq", 183}, {"printscreen", 183},
            {"rightalt", 184}, {"ralt", 184}, {"pause", 197}, {"break", 197}, {"home", 199},
            {"uparrow", 200}, {"up", 200}, {"pageup", 201}, {"pgup", 201}, {"leftarrow", 203},
            {"left", 203}, {"rightarrow", 205}, {"right", 205}, {"end", 207},
            {"downarrow", 208}, {"down", 208}, {"pagedown", 209}, {"pgdn", 209},
            {"insert", 210}, {"ins", 210}, {"delete", 211}, {"del", 211},
            {"leftmousebutton", 256}, {"leftclick", 256}, {"lclick", 256},
            {"rightmousebutton", 257}, {"rightclick", 257}, {"middlemousebutton", 258},
            {"middleclick", 258}, {"mousebutton4", 260}, {"mousebutton5", 261},
            {"mousewheelup", 264}, {"mousewheeldown", 265}
        };

        key = ToLowerCopy(key);
        if (auto it = keyMap.find(key); it != keyMap.end()) {
            return it->second;
        }
        return ParseUInt(key);
    }

    static void SetMouseInput(INPUT& input)
    {
        constexpr std::uint32_t mouseBegin = 256;
        constexpr std::uint32_t mouseEnd = 265;
        constexpr std::uint32_t xButtonBegin = 259;
        constexpr std::uint32_t wheelUp = 264;
        constexpr std::uint32_t wheelDown = 265;

        if (input.ki.wScan < mouseBegin || input.ki.wScan > mouseEnd) {
            return;
        }

        const bool keyUp = (input.ki.dwFlags & KEYEVENTF_KEYUP) != 0;
        input.type = INPUT_MOUSE;
        input.mi.dwFlags = 0;
        if (input.ki.wScan == 256) input.mi.dwFlags = keyUp ? MOUSEEVENTF_LEFTUP : MOUSEEVENTF_LEFTDOWN;
        else if (input.ki.wScan == 257) input.mi.dwFlags = keyUp ? MOUSEEVENTF_RIGHTUP : MOUSEEVENTF_RIGHTDOWN;
        else if (input.ki.wScan == 258) input.mi.dwFlags = keyUp ? MOUSEEVENTF_MIDDLEUP : MOUSEEVENTF_MIDDLEDOWN;
        else if (input.ki.wScan >= xButtonBegin && input.ki.wScan < wheelUp) {
            input.mi.dwFlags = keyUp ? MOUSEEVENTF_XUP : MOUSEEVENTF_XDOWN;
            input.mi.mouseData = input.ki.wScan - xButtonBegin;
        } else if (input.ki.wScan == wheelUp || input.ki.wScan == wheelDown) {
            input.mi.dwFlags = MOUSEEVENTF_WHEEL;
            input.mi.mouseData = input.ki.wScan == wheelUp ? WHEEL_DELTA : -WHEEL_DELTA;
        }
    }

    static void SendKey(std::uint32_t key, bool up)
    {
        INPUT input{};
        input.type = INPUT_KEYBOARD;
        input.ki.dwFlags = KEYEVENTF_SCANCODE | (up ? KEYEVENTF_KEYUP : 0);
        input.ki.wScan = static_cast<WORD>(key);
        SetMouseInput(input);
        SendInput(1, &input, sizeof(INPUT));
    }

    static void RunPressCommand(std::vector<std::string> params)
    {
        constexpr std::uint32_t defaultKeyPressTime = 50;
        std::vector<std::uint32_t> keyDown;
        std::map<std::uint32_t, std::uint32_t> keyUp;

        if ((params.size() - 1) % 2 > 0) {
            params.push_back(std::to_string(defaultKeyPressTime));
        }

        for (std::size_t i = 1; i + 1 < params.size(); i += 2) {
            const auto key = GetKeyScanCode(params[i]);
            const auto ms = ParseUInt(params[i + 1]);
            if (key == 0 || ms == 0) {
                continue;
            }
            keyDown.push_back(key);
            auto releaseMs = ms;
            while (keyUp.contains(releaseMs)) {
                ++releaseMs;
            }
            keyUp[releaseMs] = key;
        }

        for (auto key : keyDown) {
            SendKey(key, false);
        }

        std::uint32_t elapsed = 0;
        for (auto [releaseMs, key] : keyUp) {
            if (releaseMs > elapsed) {
                std::this_thread::sleep_for(std::chrono::milliseconds(releaseMs - elapsed));
                elapsed = releaseMs;
            }
            SendKey(key, true);
        }
    }

    static bool TryRunKeyCommand(const std::string& command)
    {
        auto params = SplitParams(command);
        if (params.empty()) {
            return false;
        }

        const std::string action = ToLowerCopy(params.front());
        if (action == "press") {
            RunPressCommand(std::move(params));
            return true;
        }
        if (action == "tapkey") {
            std::vector<std::string> pressParams{ "press" };
            for (auto it = std::next(params.begin()); it != params.end(); ++it) {
                pressParams.push_back(*it);
                pressParams.push_back("50");
            }
            RunPressCommand(std::move(pressParams));
            return true;
        }
        if (action == "holdkey" || action == "releasekey") {
            const bool up = action == "releasekey";
            for (auto it = std::next(params.begin()); it != params.end(); ++it) {
                const auto key = GetKeyScanCode(*it);
                if (key != 0) {
                    SendKey(key, up);
                }
            }
            return true;
        }
        if (action == "sleep") {
            if (params.size() >= 2) {
                const auto ms = ParseUInt(params[1]);
                if (ms > 0) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(ms));
                }
            }
            return true;
        }
        return false;
    }

    static bool RunConsoleCommand(const std::string& command)
    {
        auto* ui = RE::UI::GetSingleton();
        if (!ui) {
            return false;
        }

        auto menu = ui->GetMenu(RE::BSFixedString("Console"));
        if (!menu || !menu->uiMovie) {
            return false;
        }

        auto* movie = menu->uiMovie.get();
        RE::GFxValue args[3];
        args[0].SetString("ExecuteCommand");
        args[1].SetNumber(-1.0);
        args[2].SetString(command.c_str());
        return movie->Invoke("flash.external.ExternalInterface.call", nullptr, args, 3);
    }

    static bool RunConsoleCommandOnGameThread(std::string command)
    {
        auto* taskInterface = SKSE::GetTaskInterface();
        if (!taskInterface) {
            return false;
        }

        auto promise = std::make_shared<std::promise<bool>>();
        auto future = promise->get_future();
        taskInterface->AddTask([command = std::move(command), promise]() {
            promise->set_value(RunConsoleCommand(command));
        });
        return future.get();
    }

    static std::mutex& CustomCommandMutex()
    {
        static std::mutex mutex;
        return mutex;
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
    static bool ValidateShoutContext(const PipeResponse& resp, ShoutContext& ctx, std::string& reason)
    {
        // 1.  Parse base FormID + plugin
        const RE::FormID baseId = ParseHexFormID(resp.shoutFormID);
        if (baseId == 0) {
            reason = "base FormID is 0 or invalid";
            return false;
        }

        ctx.formID = ComposeRuntimeFormID(resp.shoutPlugin, baseId);
        if (ctx.formID == 0) {
            reason = "runtime FormID is 0 (plugin not found?)";
            return false;
        }
        ShoutDbg("FormID resolved: 0x" + detail::FormIDToHex(ctx.formID));

        // 2.  Power
        ctx.power = ClampPower(resp.shoutPower);
        ShoutDbg("Power (clamped): " + std::to_string(ctx.power));

        // 3.  Lookup TESShout
        ctx.shout = RE::TESForm::LookupByID<RE::TESShout>(ctx.formID);
        if (!ctx.shout) {
            reason = "TESShout not found";
            return false;
        }
        const char* name = ctx.shout->GetFullName();
        ShoutDbg("TESShout lookup: OK — \"" + std::string(name ? name : "???") + "\"");

        // 4.  Variation validity
        const int vi = ctx.power - 1;
        const auto& var = ctx.shout->variations[vi];
        ctx.spell = var.spell;

        if (!ctx.spell) {
            reason = "shout variation spell is null";
            return false;
        }
        ShoutDbg("Variation[" + std::to_string(vi) + "] spell OK");

        return true;
    }

    static bool ValidatePowerContext(const PipeResponse& resp, PowerContext& ctx, std::string& reason)
    {
        ctx.formID = ParseHexFormID(resp.powerFormID);
        if (ctx.formID == 0) {
            reason = "Power FormID is 0 or invalid";
            return false;
        }

        ctx.power = RE::TESForm::LookupByID<RE::SpellItem>(ctx.formID);
        if (!ctx.power) {
            reason = "SpellItem not found for power";
            return false;
        }

        const auto type = ctx.power->GetSpellType();
        if (type != RE::MagicSystem::SpellType::kPower && type != RE::MagicSystem::SpellType::kLesserPower) {
            reason = "SpellItem is not a power";
            return false;
        }

        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            reason = "PlayerCharacter nullptr";
            return false;
        }
        if (!player->HasSpell(ctx.power)) {
            reason = "Player does not know power";
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

    static void ExecuteVoiceTrigger(const ShoutContext& ctx, const PipeResponse& resp)
    {
        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            LogShoutCommandWarn(resp, "0x" + detail::FormIDToHex(ctx.formID), "PlayerCharacter nullptr");
            return;
        }

        const bool muteVoice = IsMuteShoutVoiceLineEnabled();

        // 1.  Equip shout
        auto* eqMgr = RE::ActorEquipManager::GetSingleton();
        if (!eqMgr) {
            LogShoutCommandWarn(resp, "0x" + detail::FormIDToHex(ctx.formID), "ActorEquipManager nullptr");
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
        std::thread([ctx, resp, muteVoice]() {
            std::this_thread::sleep_for(std::chrono::milliseconds(kEquipDelayMs));

            SKSE::GetTaskInterface()->AddTask([ctx, resp, muteVoice]() {
                // 3. Call Papyrus: DragonbornVoiceControlShout.SimulateShoutKey(power)
                auto* vm = RE::BSScript::Internal::VirtualMachine::GetSingleton();
                if (!vm) {
                    LogShoutCommandWarn(resp, "0x" + detail::FormIDToHex(ctx.formID), "Papyrus VirtualMachine singleton nullptr");
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
                    LogShoutCommandInfo(resp, ctx.formID, ctx.power, "dispatched",
                        ctx.shout->GetFullName() ? ctx.shout->GetFullName() : "???");
                } else {
                    LogShoutCommandWarn(resp, "0x" + detail::FormIDToHex(ctx.formID),
                        "DispatchStaticCall returned false (script not loaded? .pex missing?)");
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

    static void ExecutePowerTrigger(const PowerContext& ctx, const PipeResponse& resp)
    {
        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            LogVoiceCommandWarn("power", resp.trigText, resp.score, resp.powerFormID, "PlayerCharacter nullptr");
            return;
        }

        auto* eqMgr = RE::ActorEquipManager::GetSingleton();
        if (!eqMgr) {
            LogVoiceCommandWarn("power", resp.trigText, resp.score, resp.powerFormID, "ActorEquipManager nullptr");
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
            LogVoiceCommandWarn("power", resp.trigText, resp.score, resp.powerFormID, "Papyrus VirtualMachine singleton nullptr");
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
            LogVoiceCommandInfo("power", resp.trigText, resp.score, resp.powerFormID, "activated",
                ctx.power->GetFullName() ? ctx.power->GetFullName() : "???");
        } else {
            LogVoiceCommandWarn("power", resp.trigText, resp.score, resp.powerFormID,
                "DispatchStaticCall returned false for power");
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  Public entry point (called from poll thread)
    // ═══════════════════════════════════════════════════════════

    void TriggerShout(const PipeResponse& resp)
    {
        // Validate on caller thread (poll thread) — fast checks only
        ShoutContext ctx{};
        std::string reason;
        if (!ValidateShoutContext(resp, ctx, reason)) {
            LogShoutCommandWarn(resp, resp.shoutFormID, reason.empty() ? "validation failed" : reason);
            return;
        }

        // Capture context by value for the game-thread lambda
        ShoutContext captured = ctx;
        PipeResponse capturedResp = resp;

        auto* taskInterface = SKSE::GetTaskInterface();
        if (!taskInterface) {
            LogShoutCommandWarn(resp, "0x" + detail::FormIDToHex(ctx.formID), "TaskInterface nullptr");
            return;
        }

        taskInterface->AddTask([captured, capturedResp]() {
            ShoutDbg(">>> game-thread task entered");
            ExecuteVoiceTrigger(captured, capturedResp);
            ShoutDbg("<<< game-thread task exited");
        });
    }

    void TriggerPower(const PipeResponse& resp)
    {
        PowerContext ctx{};
        std::string reason;
        if (!ValidatePowerContext(resp, ctx, reason)) {
            LogVoiceCommandWarn("power", resp.trigText, resp.score, resp.powerFormID,
                reason.empty() ? "validation failed" : reason);
            return;
        }

        PowerContext captured = ctx;
        PipeResponse capturedResp = resp;
        if (auto* t = SKSE::GetTaskInterface(); t) {
            t->AddTask([captured, capturedResp]() {
                ExecutePowerTrigger(captured, capturedResp);
            });
        } else {
            LogVoiceCommandWarn("power", resp.trigText, resp.score, resp.powerFormID,
                "TaskInterface nullptr");
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  Weapon trigger:  Equip weapon to requested hand (right by default)
    // ═══════════════════════════════════════════════════════════

    void TriggerWeapon(const PipeResponse& resp)
    {
        RE::FormID formID = ParseHexFormID(resp.itemFormID);
        if (formID == 0) {
            LogVoiceCommandWarn("weapon", resp.trigText, resp.score, resp.itemFormID,
                "Weapon FormID is 0 or invalid");
            return;
        }

        PipeResponse capturedResp = resp;
        if (auto* t = SKSE::GetTaskInterface(); t) {
            t->AddTask([formID, capturedResp]() {
                auto* player = RE::PlayerCharacter::GetSingleton();
                if (!player) {
                    LogVoiceCommandWarn("weapon", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "PlayerCharacter nullptr");
                    return;
                }

                auto* weap = RE::TESForm::LookupByID<RE::TESObjectWEAP>(formID);
                if (!weap) {
                    LogVoiceCommandWarn("weapon", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "TESObjectWEAP not found");
                    return;
                }

                const int inventoryCount = GetInventoryItemCount(player, formID);
                if (inventoryCount <= 0) {
                    LogVoiceCommandWarn("weapon", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "Player does not have weapon");
                    return;
                }

                const char* name = weap->GetFullName();
                auto* eqMgr = RE::ActorEquipManager::GetSingleton();
                if (!eqMgr) {
                    LogVoiceCommandWarn("weapon", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "ActorEquipManager nullptr");
                    return;
                }

                const auto equipOneHand = [&](const std::string& hand) -> bool {
                    if (IsObjectEquippedInRequestedHand(player, formID, hand)) {
                        return true;
                    }

                    auto* slot = GetHandEquipSlot(hand);
                    if (!slot) {
                        LogVoiceCommandWarn("weapon", capturedResp.trigText, capturedResp.score,
                            capturedResp.itemFormID, "Equip slot not found");
                        return false;
                    }

                    const int equippedCount = CountEquippedObject(player, formID);
                    if (equippedCount >= inventoryCount &&
                        IsObjectEquippedInOppositeHand(player, formID, hand)) {
                        auto* oppositeSlot = GetHandEquipSlot(IsLeftItemHand(hand) ? "right" : "left");
                        if (!oppositeSlot) {
                            LogVoiceCommandWarn("weapon", capturedResp.trigText, capturedResp.score,
                                capturedResp.itemFormID, "Opposite equip slot not found");
                            return false;
                        }
                        eqMgr->UnequipObject(player, weap, nullptr, 1, oppositeSlot);
                    }

                    eqMgr->EquipObject(player, weap, nullptr, 1, slot);
                    return true;
                };

                if (IsBothItemHands(capturedResp.itemEquipHand)) {
                    if (!equipOneHand("right")) {
                        return;
                    }
                    if (CountEquippedObject(player, formID) < inventoryCount) {
                        if (!equipOneHand("left")) {
                            return;
                        }
                    }
                } else if (!equipOneHand(capturedResp.itemEquipHand)) {
                    return;
                }

                LogVoiceCommandInfo("weapon", capturedResp.trigText, capturedResp.score,
                    capturedResp.itemFormID, "equipped", name ? name : "???", NormalizeItemHand(capturedResp.itemEquipHand));
            });
        } else {
            LogVoiceCommandWarn("weapon", resp.trigText, resp.score, resp.itemFormID,
                "TaskInterface nullptr");
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  Spell trigger:  Equip spell to requested hand (right by default)
    // ═══════════════════════════════════════════════════════════

    void TriggerSpell(const PipeResponse& resp)
    {
        RE::FormID formID = ParseHexFormID(resp.itemFormID);
        if (formID == 0) {
            LogVoiceCommandWarn("spell", resp.trigText, resp.score, resp.itemFormID,
                "Spell FormID is 0 or invalid");
            return;
        }

        PipeResponse capturedResp = resp;
        if (auto* t = SKSE::GetTaskInterface(); t) {
            t->AddTask([formID, capturedResp]() {
                auto* player = RE::PlayerCharacter::GetSingleton();
                if (!player) {
                    LogVoiceCommandWarn("spell", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "PlayerCharacter nullptr");
                    return;
                }

                auto* spell = RE::TESForm::LookupByID<RE::SpellItem>(formID);
                if (!spell) {
                    LogVoiceCommandWarn("spell", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "SpellItem not found");
                    return;
                }

                if (!player->HasSpell(spell)) {
                    LogVoiceCommandWarn("spell", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "Player does not know spell");
                    return;
                }

                auto* eqMgr = RE::ActorEquipManager::GetSingleton();
                if (!eqMgr) {
                    LogVoiceCommandWarn("spell", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "ActorEquipManager nullptr");
                    return;
                }

                if (IsBothItemHands(capturedResp.itemEquipHand)) {
                    auto* rightSlot = GetHandEquipSlot("right");
                    auto* leftSlot = GetHandEquipSlot("left");
                    if (!rightSlot || !leftSlot) {
                        LogVoiceCommandWarn("spell", capturedResp.trigText, capturedResp.score,
                            capturedResp.itemFormID, "Equip slot not found");
                        return;
                    }

                    eqMgr->EquipSpell(player, spell, rightSlot);
                    eqMgr->EquipSpell(player, spell, leftSlot);
                } else {
                    auto* slot = GetHandEquipSlot(capturedResp.itemEquipHand);
                    if (!slot) {
                        LogVoiceCommandWarn("spell", capturedResp.trigText, capturedResp.score,
                            capturedResp.itemFormID, "Equip slot not found");
                        return;
                    }

                    eqMgr->EquipSpell(player, spell, slot);
                }

                const char* name = spell->GetFullName();
                LogVoiceCommandInfo("spell", capturedResp.trigText, capturedResp.score,
                    capturedResp.itemFormID, "equipped", name ? name : "???", NormalizeItemHand(capturedResp.itemEquipHand));
            });
        } else {
            LogVoiceCommandWarn("spell", resp.trigText, resp.score, resp.itemFormID,
                "TaskInterface nullptr");
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  Potion trigger:  Use a potion from inventory
    // ═══════════════════════════════════════════════════════════

    void TriggerPotion(const PipeResponse& resp)
    {
        RE::FormID formID = ParseHexFormID(resp.itemFormID);
        if (formID == 0) {
            LogVoiceCommandWarn("potion", resp.trigText, resp.score, resp.itemFormID,
                "Potion FormID is 0 or invalid");
            return;
        }

        PipeResponse capturedResp = resp;
        if (auto* t = SKSE::GetTaskInterface(); t) {
            t->AddTask([formID, capturedResp]() {
                auto* player = RE::PlayerCharacter::GetSingleton();
                if (!player) {
                    LogVoiceCommandWarn("potion", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "PlayerCharacter nullptr");
                    return;
                }

                auto* alch = RE::TESForm::LookupByID<RE::AlchemyItem>(formID);
                if (!alch) {
                    LogVoiceCommandWarn("potion", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "AlchemyItem not found");
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
                    LogVoiceCommandWarn("potion", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "Player does not have potion");
                    return;
                }

                auto* eqMgr = RE::ActorEquipManager::GetSingleton();
                if (!eqMgr) {
                    LogVoiceCommandWarn("potion", capturedResp.trigText, capturedResp.score,
                        capturedResp.itemFormID, "ActorEquipManager nullptr");
                    return;
                }

                // EquipObject with a potion triggers consumption
                eqMgr->EquipObject(player, alch);

                const char* name = alch->GetFullName();
                LogVoiceCommandInfo("potion", capturedResp.trigText, capturedResp.score,
                    capturedResp.itemFormID, "used", name ? name : "???");
            });
        } else {
            LogVoiceCommandWarn("potion", resp.trigText, resp.score, resp.itemFormID,
                "TaskInterface nullptr");
        }
    }

    void TriggerCustomCommands(const PipeResponse& resp)
    {
        if (resp.customCommands.empty()) {
            LogWarn("Voice command recognized=" + Quote(resp.trigText) +
                    " result=failed kind=custom reason=\"no commands\" score=" + FormatScore(resp.score));
            return;
        }

        PipeResponse capturedResp = resp;
        std::thread([capturedResp]() {
            std::scoped_lock lock(CustomCommandMutex());
            std::size_t executed = 0;
            for (const auto& raw : capturedResp.customCommands) {
                const std::string command = TrimCopy(raw);
                if (command.empty()) {
                    continue;
                }

                if (TryRunKeyCommand(command)) {
                    ++executed;
                    continue;
                }

                if (RunConsoleCommandOnGameThread(command)) {
                    ++executed;
                } else {
                    LogWarn("Voice command recognized=" + Quote(capturedResp.trigText) +
                            " result=failed kind=custom command=" + Quote(command) +
                            " reason=\"Console menu unavailable\" score=" + FormatScore(capturedResp.score));
                }
            }

            LogInfo("Voice command recognized=" + Quote(capturedResp.trigText) +
                    " result=executed kind=custom commands=" + std::to_string(executed) +
                    " score=" + FormatScore(capturedResp.score));
        }).detach();
    }

} // namespace DragonbornVoiceControl
