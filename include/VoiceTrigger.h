#pragma once

#include "PipeClient.h"

namespace DragonbornVoiceControl
{
    // ──────────────────────────────────────────────────────
    //  Shout triggering uses Papyrus Input.HoldKey/ReleaseKey
    //  via DragonbornVoiceControlShout.SimulateShoutKey().
    //  The engine handles cooldown, menu, power tier natively.
    //
    //  Voice-line muting works by capturing the player's
    //  AIProcess sound handles before the shout fires, then
    //  stopping any NEW sounds on the player afterwards.
    //  Player-specific — does not affect NPC audio.
    // ──────────────────────────────────────────────────────

    /// Called from HandleVoiceTrigger on the poll thread.
    void TriggerShout(const PipeResponse& resp);

    /// Called from HandlePowerTrigger on the poll thread.
    void TriggerPower(const PipeResponse& resp);

    /// Called from HandleWeaponTrigger — equips a weapon to right hand.
    void TriggerWeapon(const PipeResponse& resp);

    /// Called from HandleSpellTrigger — equips a spell to right hand.
    void TriggerSpell(const PipeResponse& resp);

    /// Called from HandlePotionTrigger — uses a potion from inventory.
    void TriggerPotion(const PipeResponse& resp);

    /// Called from HandleCustomCommandTrigger — runs configured console/key commands.
    void TriggerCustomCommands(const PipeResponse& resp);
}
