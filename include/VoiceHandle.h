#pragma once

#include "PipeClient.h"

namespace DragonbornVoiceControl
{
    bool CanUseShoutNow();
    void HandleVoiceTrigger(const PipeResponse& resp);
    void HandlePowerTrigger(const PipeResponse& resp);
    void HandleWeaponTrigger(const PipeResponse& resp);
    void HandleSpellTrigger(const PipeResponse& resp);
    void HandlePotionTrigger(const PipeResponse& resp);
    void HandleCustomCommandTrigger(const PipeResponse& resp);

    void SetGameLoaded(bool loaded);
    bool IsGameLoaded();
}
