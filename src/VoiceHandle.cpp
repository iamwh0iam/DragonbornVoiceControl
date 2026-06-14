#include "PCH.h"

#include "VoiceHandle.h"
#include "ShoutsInternal.h"
#include "VoiceTrigger.h"

#include "Common.h"
#include "Logging.h"
#include "Settings.h"

#include <string>

namespace DragonbornVoiceControl::detail
{
    std::mutex g_shoutCacheMutex;
    std::unordered_set<RE::FormID> g_knownShouts;
    std::unordered_set<RE::FormID> g_favoriteShouts;
    std::unordered_set<RE::FormID> g_knownPowers;
    std::unordered_set<RE::FormID> g_favoritePowers;
    std::atomic<double> g_voiceCooldownEndSec{ 0.0 };

    std::atomic_bool g_gameLoaded{ false };
    std::atomic_bool g_shoutScanInFlight{ false };
    std::atomic_bool g_powerScanInFlight{ false };
    std::uint64_t g_lastShoutStateHash = 0;
    std::uint64_t g_lastPowerStateHash = 0;

    std::atomic_bool g_muteShoutVoiceWindow{ false };
    std::atomic<std::uint64_t> g_muteShoutVoiceWindowGen{ 0 };
}

namespace DragonbornVoiceControl
{
    bool CanUseShoutNow()
    {
        if (!IsVoiceShoutsEnabled()) {
            return false;
        }

        return (detail::g_voiceCooldownEndSec.load() - GetNowSec()) <= 0.0;
    }

    void HandleVoiceTrigger(const PipeResponse& resp)
    {
        TriggerShout(resp);
    }

    void HandlePowerTrigger(const PipeResponse& resp)
    {
        TriggerPower(resp);
    }

    void HandleWeaponTrigger(const PipeResponse& resp)
    {
        TriggerWeapon(resp);
    }

    void HandleSpellTrigger(const PipeResponse& resp)
    {
        TriggerSpell(resp);
    }

    void HandlePotionTrigger(const PipeResponse& resp)
    {
        TriggerPotion(resp);
    }

    void HandleCustomCommandTrigger(const PipeResponse& resp)
    {
        TriggerCustomCommands(resp);
    }

    void SetGameLoaded(bool loaded)
    {
        detail::g_gameLoaded.store(loaded);
    }

    bool IsGameLoaded()
    {
        return detail::g_gameLoaded.load();
    }
}
