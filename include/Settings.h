#pragma once

#include <string_view>

namespace RE
{
    namespace BSScript
    {
        class IVirtualMachine;
    }
}

namespace SKSE
{
    class SerializationInterface;
}

namespace DragonbornVoiceControl
{
    struct Settings
    {
        bool enableVoiceOpen{ true };
        bool enableVoiceClose{ true };
        bool enableDialogueSelect{ true };
        bool enableVoiceShouts{ true };
        bool enablePowers{ false };
        bool muteShoutVoiceLine{ true };
        bool enableWeapons{ false };
        bool enableSpells{ false };
        bool enablePotions{ false };
        bool quickUsePotions{ true };
        bool useBestPotion{ true };
        bool specifyHand{ true };
        bool quickEquip{ true };
        bool enableKeyConsole{ false };
        bool enablePauseResumePhrases{ false };
        bool debug{ false };
        bool debugUnrecognized{ true };
        bool saveWavCaptures{ false };
    };

    bool IsVoiceOpenEnabled();
    bool IsVoiceCloseEnabled();
    bool IsDialogueSelectEnabled();
    bool IsVoiceShoutsEnabled();
    bool IsEnablePowersEnabled();
    bool IsMuteShoutVoiceLineEnabled();
    bool IsWeaponsEnabled();
    bool IsSpellsEnabled();
    bool IsPotionsEnabled();
    bool IsQuickUsePotionsEnabled();
    bool IsUseBestPotionEnabled();
    bool IsSpecifyHandEnabled();
    bool IsQuickEquipEnabled();
    bool IsKeyConsoleEnabled();
    bool IsPauseResumePhrasesEnabled();
    bool IsDebugEnabled();
    bool IsSaveWavCapturesEnabled();

    void DebugNotify(std::string_view msg);

    void ResetToDefaultsForNewGame();

    void SaveSettings(SKSE::SerializationInterface* serde);
    void LoadSettings(SKSE::SerializationInterface* serde);

    bool RegisterPapyrus(RE::BSScript::IVirtualMachine* vm);
}
