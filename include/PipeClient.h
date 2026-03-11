// PipeClient.h
#pragma once
#include <atomic>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

struct PipeResponse
{
    int index = -1;
    float score = 0.0f;
    std::string type;      // "RES" or "TRIG" or "DBG"
    std::string trigKind;  // for TRIG: "open" / "shout" / "power" / "weapon" / "spell" / "potion"
    std::string trigText;  // recognized text

    // Shout-specific fields (for TRIG|shout)
    std::string shoutPlugin;  // e.g. "Skyrim.esm"
    std::string shoutFormID;  // base formid, e.g. "0x0013E09"
    int shoutPower = 0;       // 1..3

    // Power-specific fields (for TRIG|power)
    std::string powerFormID;  // e.g. "0x00012ABC"

    // Generic item field (for TRIG|weapon, TRIG|spell, TRIG|potion)
    std::string itemFormID;
};

struct ShoutEntry
{
    std::string plugin;
    std::string formIdHex;  // base formid (0x00XXXXXX)
    std::string name;
    std::string editorID;
};

struct PowerEntry
{
    std::string formIdHex;
    std::string name;
};

struct ItemEntry
{
    std::string formIdHex;
    std::string name;
};

class PipeClient
{
public:
    static PipeClient& Get();

    void Start();
    void Stop();
    ~PipeClient();

    void SendOptions(const std::vector<std::string>& options);
    void SendClose();
    void SendListen(bool on);
    void SendListenCommands(bool on);
    void SendShoutContext(bool allowed);
    void SendPlayerDrawnState(bool drawn);
    void SendPlayerCombatState(bool inCombat);
    void SendGameLanguage(const std::string& langCode);

    // Sticky runtime config toggles (resent after reconnect and only when changed).
    void SendConfigOpen(bool enabled);
    void SendConfigClose(bool enabled);
    void SendConfigShouts(bool enabled);
    void SendConfigDebug(bool enabled);
    void SendConfigSaveWav(bool enabled);
    void SendConfigDialogueSelect(bool enabled);
    void SendConfigWeapons(bool enabled);
    void SendConfigSpells(bool enabled);
    void SendConfigPowers(bool enabled);
    void SendConfigPotions(bool enabled);

    // Favorites grammar sync.
    void SendShoutsAllowed(const std::vector<ShoutEntry>& shouts);
    void SendPowersAllowed(const std::vector<PowerEntry>& powers);
    void SendWeaponsAllowed(const std::vector<ItemEntry>& weapons);
    void SendSpellsAllowed(const std::vector<ItemEntry>& spells);
    void SendPotionsAllowed(const std::vector<ItemEntry>& potions);

    // Favorites batch sync.
    void SendAllFavorites(
        const std::vector<ShoutEntry>& shouts,
        const std::vector<PowerEntry>& powers,
        const std::vector<ItemEntry>& weapons,
        const std::vector<ItemEntry>& spells,
        const std::vector<ItemEntry>& potions);

    std::optional<PipeResponse> ConsumeLastResponse();
    std::optional<bool> ConsumeConnectionEvent();

private:
    PipeClient() = default;
    void ThreadMain();
    void HandleDisconnect();

    bool WriteLine(const std::string& line);
    void ProcessIncoming();

    std::thread _thread;
    std::atomic<bool> _running{ false };
    std::atomic<bool> _connected{ false };

    void* _pipe = nullptr;  // HANDLE

    std::string _recvBuf;

    std::mutex _sendMutex;
    std::optional<std::vector<std::string>> _pendingOptions;
    std::optional<std::vector<std::string>> _pendingFavorites;
    bool _pendingClose{ false };
    std::optional<bool> _pendingListen;
    std::optional<bool> _pendingListenCommands;
    std::optional<bool> _desiredShoutContext;
    std::optional<bool> _desiredPlayerDrawn;
    std::optional<bool> _desiredPlayerCombat;
    std::optional<std::string> _desiredGameLang;
    // sticky config desired states
    std::optional<bool> _desiredCfgOpen;
    std::optional<bool> _desiredCfgClose;
    std::optional<bool> _desiredCfgVoiceHandle;
    std::optional<bool> _desiredCfgDebug;
    std::optional<bool> _desiredCfgSaveWav;
    std::optional<bool> _desiredCfgDialogueSelect;
    std::optional<bool> _desiredCfgWeapons;
    std::optional<bool> _desiredCfgSpells;
    std::optional<bool> _desiredCfgPowers;
    std::optional<bool> _desiredCfgPotions;

    // last-sent states (to avoid spamming)
    std::optional<bool> _lastSentCfgOpen;
    std::optional<bool> _lastSentCfgClose;
    std::optional<bool> _lastSentCfgVoiceHandle;
    std::optional<bool> _lastSentCfgDebug;
    std::optional<bool> _lastSentCfgSaveWav;
    std::optional<bool> _lastSentCfgDialogueSelect;
    std::optional<bool> _lastSentCfgWeapons;
    std::optional<bool> _lastSentCfgSpells;
    std::optional<bool> _lastSentCfgPowers;
    std::optional<bool> _lastSentCfgPotions;
    std::optional<bool> _lastSentShoutContext;
    std::optional<bool> _lastSentPlayerDrawn;
    std::optional<bool> _lastSentPlayerCombat;
    std::optional<std::string> _lastSentGameLang;

    std::mutex _respMutex;
    std::deque<PipeResponse> _responses;

    std::mutex _connMutex;
    std::optional<bool> _connEvent;
};
