#include "PipeClient.h"

#include "Logging.h"

#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <cctype>
#include <sstream>
#include <SKSE/SKSE.h>

static constexpr const wchar_t* PIPE_NAME = L"\\\\.\\pipe\\DVC_voice_local";

static void SanitizeInPlace(std::string& s);
static void SanitizeFavoriteField(std::string& s);

PipeClient::~PipeClient()
{
    Stop();
}

PipeClient& PipeClient::Get()
{
    static PipeClient inst;
    return inst;
}

void PipeClient::Start()
{
    if (_running.load()) {
        return;
    }
    _running = true;
    _thread = std::thread(&PipeClient::ThreadMain, this);
}

void PipeClient::Stop()
{
    _running = false;

    if (_pipe) {
        CloseHandle((HANDLE)_pipe);
        _pipe = nullptr;
    }

    _connected = false;
    _clientReady = false;

    if (_thread.joinable()) {
        _thread.join();
    }
}

void PipeClient::SendOptions(const std::vector<std::string>& options)
{
    std::lock_guard lg(_sendMutex);
    _pendingOptions = options;
}

void PipeClient::SendClose()
{
    std::lock_guard lg(_sendMutex);
    _pendingClose = true;
}

void PipeClient::SendListen(bool on)
{
    std::lock_guard lg(_sendMutex);
    _pendingListen = on;
}

void PipeClient::SendShoutContext(bool allowed)
{
    std::lock_guard lg(_sendMutex);
    _desiredShoutContext = allowed;
}

void PipeClient::SendPlayerDrawnState(bool drawn)
{
    std::lock_guard lg(_sendMutex);
    _desiredPlayerDrawn = drawn;
}

void PipeClient::SendPlayerCombatState(bool inCombat)
{
    std::lock_guard lg(_sendMutex);
    _desiredPlayerCombat = inCombat;
}

void PipeClient::SendBlockingMenuState(bool blocked)
{
    std::lock_guard lg(_sendMutex);
    _desiredBlockingMenu = blocked;
}

void PipeClient::SendGameLanguage(const std::string& langCode)
{
    std::string s = langCode;
    SanitizeInPlace(s);
    if (s.empty()) {
        return;
    }

    std::lock_guard lg(_sendMutex);
    _desiredGameLang = s;
}

void PipeClient::SendConfigOpen(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgOpen = enabled;
}

void PipeClient::SendConfigClose(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgClose = enabled;
}

void PipeClient::SendConfigShouts(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgVoiceHandle = enabled;
}

void PipeClient::SendConfigDebug(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgDebug = enabled;
}

void PipeClient::SendConfigSaveWav(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgSaveWav = enabled;
}

void PipeClient::SendConfigDialogueSelect(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgDialogueSelect = enabled;
}

void PipeClient::SendConfigWeapons(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgWeapons = enabled;
}

void PipeClient::SendConfigSpells(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgSpells = enabled;
}

void PipeClient::SendConfigPowers(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgPowers = enabled;
}

void PipeClient::SendConfigPotions(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgPotions = enabled;
}

void PipeClient::SendConfigPotionsQuickUse(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgPotionsQuickUse = enabled;
}

void PipeClient::SendConfigPotionsBestPotion(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgPotionsBestPotion = enabled;
}

void PipeClient::SendConfigSpecifyHand(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgSpecifyHand = enabled;
}

void PipeClient::SendConfigQuickEquip(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgQuickEquip = enabled;
}

void PipeClient::SendConfigKeyConsole(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgKeyConsole = enabled;
}

void PipeClient::SendConfigPauseResume(bool enabled)
{
    std::lock_guard lg(_sendMutex);
    _desiredCfgPauseResume = enabled;
}

void PipeClient::SendShoutsAllowed(const std::vector<ShoutEntry>& shouts)
{
    (void)shouts;
}

void PipeClient::SendPowersAllowed(const std::vector<PowerEntry>& powers)
{
    (void)powers;
}

void PipeClient::SendWeaponsAllowed(const std::vector<ItemEntry>& weapons)
{
    (void)weapons;
}

void PipeClient::SendSpellsAllowed(const std::vector<ItemEntry>& spells)
{
    (void)spells;
}

void PipeClient::SendPotionsAllowed(const std::vector<PotionEntry>& potions)
{
    (void)potions;
}

void PipeClient::SendAllFavorites(
    const std::vector<ShoutEntry>& shouts,
    const std::vector<PowerEntry>& powers,
    const std::vector<ItemEntry>& weapons,
    const std::vector<ItemEntry>& spells,
    const std::vector<PotionEntry>& potions)
{
    std::vector<std::string> lines;
    lines.reserve(2 + shouts.size() + powers.size() + weapons.size() + spells.size() + potions.size());

    lines.push_back("FAV|BEGIN");

    for (const auto& entry : shouts) {
        std::string plugin = entry.plugin;
        std::string formId = entry.formIdHex;
        std::string name = entry.name;
        std::string editorId = entry.editorID;
        SanitizeFavoriteField(plugin);
        SanitizeInPlace(formId);
        SanitizeFavoriteField(name);
        SanitizeFavoriteField(editorId);
        lines.push_back("FAV|SHOUT|" + plugin + "|" + formId + "|" + name + "|" + editorId);
    }

    for (const auto& entry : powers) {
        std::string formId = entry.formIdHex;
        std::string name = entry.name;
        SanitizeInPlace(formId);
        SanitizeFavoriteField(name);
        lines.push_back("FAV|POWER|" + formId + "|" + name);
    }

    for (const auto& entry : weapons) {
        std::string formId = entry.formIdHex;
        std::string name = entry.name;
        SanitizeInPlace(formId);
        SanitizeFavoriteField(name);
        lines.push_back("FAV|WEAPON|" + formId + "|" + name);
    }

    for (const auto& entry : spells) {
        std::string formId = entry.formIdHex;
        std::string name = entry.name;
        SanitizeInPlace(formId);
        SanitizeFavoriteField(name);
        lines.push_back("FAV|SPELL|" + formId + "|" + name);
    }

    for (const auto& entry : potions) {
        std::string formId = entry.formIdHex;
        std::string name = entry.name;
        SanitizeInPlace(formId);
        SanitizeFavoriteField(name);
        lines.push_back("FAV|POTION|" + formId + "|" + name + "|" +
            std::to_string(entry.healthRestorePower) + "|" +
            std::to_string(entry.magickaRestorePower) + "|" +
            std::to_string(entry.staminaRestorePower));
    }

    lines.push_back("FAV|END");

    {
        std::lock_guard lg(_sendMutex);
        _pendingFavorites = std::move(lines);
    }
}

std::optional<PipeResponse> PipeClient::ConsumeLastResponse()
{
    std::lock_guard lg(_respMutex);
    if (_responses.empty()) {
        return std::nullopt;
    }
    PipeResponse r = std::move(_responses.front());
    _responses.pop_front();
    return r;
}

std::optional<bool> PipeClient::ConsumeConnectionEvent()
{
    std::lock_guard lg(_connMutex);
    auto e = _connEvent;
    _connEvent.reset();
    return e;
}

std::optional<bool> PipeClient::ConsumeClientReadyEvent()
{
    std::lock_guard lg(_connMutex);
    auto e = _readyEvent;
    _readyEvent.reset();
    return e;
}

bool PipeClient::IsClientReady() const
{
    return _clientReady.load();
}

bool PipeClient::WriteLine(const std::string& line)
{
    if (!_connected || !_pipe) {
        return false;
    }

    std::string payload = line;
    payload.push_back('\n');

    DWORD written = 0;
    BOOL ok = WriteFile((HANDLE)_pipe, payload.data(), (DWORD)payload.size(), &written, nullptr);
    return ok && written == payload.size();
}

static void SanitizeInPlace(std::string& s)
{
    for (char& c : s) {
        if (c == '\n' || c == '\r') {
            c = ' ';
        }
    }
}

static void SanitizeFavoriteField(std::string& s)
{
    SanitizeInPlace(s);
    for (char& c : s) {
        if (c == '|') {
            c = ' ';
        }
    }
}

static int Base64Value(char c)
{
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+' || c == '-') return 62;
    if (c == '/' || c == '_') return 63;
    return -1;
}

static std::string DecodeBase64Url(const std::string& input)
{
    std::string out;
    int val = 0;
    int bits = -8;
    for (char c : input) {
        if (c == '=') {
            break;
        }
        const int d = Base64Value(c);
        if (d < 0) {
            continue;
        }
        val = (val << 6) | d;
        bits += 6;
        if (bits >= 0) {
            out.push_back(static_cast<char>((val >> bits) & 0xFF));
            bits -= 8;
        }
    }
    return out;
}

static std::vector<std::string> ParseJsonStringArray(const std::string& json)
{
    std::vector<std::string> out;
    bool inString = false;
    bool escape = false;
    std::string current;

    for (char c : json) {
        if (!inString) {
            if (c == '"') {
                inString = true;
                current.clear();
            }
            continue;
        }

        if (escape) {
            switch (c) {
            case '"': current.push_back('"'); break;
            case '\\': current.push_back('\\'); break;
            case '/': current.push_back('/'); break;
            case 'b': current.push_back('\b'); break;
            case 'f': current.push_back('\f'); break;
            case 'n': current.push_back('\n'); break;
            case 'r': current.push_back('\r'); break;
            case 't': current.push_back('\t'); break;
            default: current.push_back(c); break;
            }
            escape = false;
            continue;
        }

        if (c == '\\') {
            escape = true;
        } else if (c == '"') {
            inString = false;
            out.push_back(current);
        } else {
            current.push_back(c);
        }
    }

    return out;
}

void PipeClient::ProcessIncoming()
{
    if (!_connected || !_pipe) {
        return;
    }

    DWORD avail = 0;
    BOOL okPeek = PeekNamedPipe((HANDLE)_pipe, nullptr, 0, nullptr, &avail, nullptr);
    if (!okPeek) {
        HandleDisconnect();
        return;
    }
    if (avail == 0) {
        return;
    }

    std::string chunk;
    chunk.resize(avail);

    DWORD readBytes = 0;
    BOOL okRead = ReadFile((HANDLE)_pipe, chunk.data(), avail, &readBytes, nullptr);
    if (!okRead || readBytes == 0) {
        HandleDisconnect();
        return;
    }
    chunk.resize(readBytes);

    _recvBuf += chunk;

    for (;;) {
        auto pos = _recvBuf.find('\n');
        if (pos == std::string::npos) {
            break;
        }

        std::string line = _recvBuf.substr(0, pos);
        _recvBuf.erase(0, pos + 1);

        if (line.rfind("RES|", 0) == 0) {
            PipeResponse resp;
            resp.type = "RES";
            try {
                size_t p1 = line.find('|', 4);
                if (p1 != std::string::npos) {
                    resp.index = std::stoi(line.substr(4, p1 - 4));
                    resp.score = std::stof(line.substr(p1 + 1));
                }
            } catch (...) {
                resp.index = -1;
                resp.score = 0.0f;
            }

            {
                std::lock_guard lg(_respMutex);
                _responses.push_back(resp);
                if (_responses.size() > 128) {
                    _responses.pop_front();
                }
            }

            DragonbornVoiceControl::LogDebug("[DVC_SERVER] recv RES index=" + std::to_string(resp.index) +
                " score=" + std::to_string(resp.score));
        } else if (line.rfind("TRIG|", 0) == 0) {
            PipeResponse resp;
            resp.type = "TRIG";
            try {
                size_t p1 = line.find('|', 5);
                if (p1 != std::string::npos) {
                    resp.trigKind = line.substr(5, p1 - 5);

                    if (resp.trigKind == "shout") {
                        size_t p2 = line.find('|', p1 + 1);
                        if (p2 != std::string::npos) {
                            resp.shoutPlugin = line.substr(p1 + 1, p2 - p1 - 1);
                            size_t p3 = line.find('|', p2 + 1);
                            if (p3 != std::string::npos) {
                                resp.shoutFormID = line.substr(p2 + 1, p3 - p2 - 1);
                                size_t p4 = line.find('|', p3 + 1);
                                if (p4 != std::string::npos) {
                                    resp.shoutPower = std::stoi(line.substr(p3 + 1, p4 - p3 - 1));
                                    size_t p5 = line.find('|', p4 + 1);
                                    if (p5 != std::string::npos) {
                                        resp.score = std::stof(line.substr(p4 + 1, p5 - p4 - 1));
                                        resp.trigText = line.substr(p5 + 1);
                                    }
                                }
                            }
                        }
                    } else if (resp.trigKind == "power") {
                        size_t p2 = line.find('|', p1 + 1);
                        if (p2 != std::string::npos) {
                            resp.powerFormID = line.substr(p1 + 1, p2 - p1 - 1);
                            size_t p3 = line.find('|', p2 + 1);
                            if (p3 != std::string::npos) {
                                resp.score = std::stof(line.substr(p2 + 1, p3 - p2 - 1));
                                resp.trigText = line.substr(p3 + 1);
                            }
                        }
                    } else if (resp.trigKind == "weapon" || resp.trigKind == "spell" || resp.trigKind == "potion") {
                        // Legacy: TRIG|weapon|formid|score|text
                        // New weapon/spell: TRIG|weapon|formid|hand|score|text
                        size_t p2 = line.find('|', p1 + 1);
                        if (p2 != std::string::npos) {
                            resp.itemFormID = line.substr(p1 + 1, p2 - p1 - 1);
                            size_t p3 = line.find('|', p2 + 1);
                            if (p3 != std::string::npos) {
                                std::string scoreOrHand = line.substr(p2 + 1, p3 - p2 - 1);
                                if ((resp.trigKind == "weapon" || resp.trigKind == "spell") &&
                                    (scoreOrHand == "left" || scoreOrHand == "right" || scoreOrHand == "both")) {
                                    resp.itemEquipHand = scoreOrHand;
                                    size_t p4 = line.find('|', p3 + 1);
                                    if (p4 != std::string::npos) {
                                        resp.score = std::stof(line.substr(p3 + 1, p4 - p3 - 1));
                                        resp.trigText = line.substr(p4 + 1);
                                    }
                                } else {
                                    resp.itemEquipHand = "right";
                                    resp.score = std::stof(scoreOrHand);
                                    resp.trigText = line.substr(p3 + 1);
                                }
                            }
                        }
                    } else if (resp.trigKind == "custom") {
                        size_t p2 = line.find('|', p1 + 1);
                        if (p2 != std::string::npos) {
                            resp.score = std::stof(line.substr(p1 + 1, p2 - p1 - 1));
                            size_t p3 = line.find('|', p2 + 1);
                            if (p3 != std::string::npos) {
                                resp.trigText = line.substr(p2 + 1, p3 - p2 - 1);
                                resp.customCommands = ParseJsonStringArray(DecodeBase64Url(line.substr(p3 + 1)));
                            } else {
                                resp.trigText = line.substr(p2 + 1);
                            }
                        }
                    } else {
                        size_t p2 = line.find('|', p1 + 1);
                        if (p2 != std::string::npos) {
                            resp.score = std::stof(line.substr(p1 + 1, p2 - p1 - 1));
                            resp.trigText = line.substr(p2 + 1);
                        }
                    }
                }
            } catch (...) {
                resp.score = 0.0f;
            }

            {
                std::lock_guard lg(_respMutex);
                _responses.push_back(resp);
                if (_responses.size() > 128) {
                    _responses.pop_front();
                }
            }

            if (resp.trigKind == "shout") {
                DragonbornVoiceControl::LogDebug("[DVC_SERVER] recv TRIG shout formid=" + resp.shoutFormID +
                    " power=" + std::to_string(resp.shoutPower) +
                    " score=" + std::to_string(resp.score) +
                    " text=" + resp.trigText);
            } else if (resp.trigKind == "power") {
                DragonbornVoiceControl::LogDebug("[DVC_SERVER] recv TRIG power formid=" + resp.powerFormID +
                    " score=" + std::to_string(resp.score) +
                    " text=" + resp.trigText);
            } else if (resp.trigKind == "weapon" || resp.trigKind == "spell" || resp.trigKind == "potion") {
                DragonbornVoiceControl::LogDebug("[DVC_SERVER] recv TRIG " + resp.trigKind +
                    " formid=" + resp.itemFormID +
                    " hand=" + resp.itemEquipHand +
                    " score=" + std::to_string(resp.score) +
                    " text=" + resp.trigText);
            } else if (resp.trigKind == "custom") {
                DragonbornVoiceControl::LogDebug("[DVC_SERVER] recv TRIG custom commands=" +
                    std::to_string(resp.customCommands.size()) +
                    " score=" + std::to_string(resp.score) +
                    " text=" + resp.trigText);
            } else {
                DragonbornVoiceControl::LogDebug("[DVC_SERVER] recv TRIG kind=" + resp.trigKind +
                    " score=" + std::to_string(resp.score) +
                    " text=" + resp.trigText);
            }
        } else if (line == "READY|CLIENT") {
            _clientReady.store(true);
            {
                std::lock_guard lg(_connMutex);
                _readyEvent = true;
            }
            DragonbornVoiceControl::LogInfo("[DVC_SERVER] Server Ready");
        } else if (line.rfind("DBG|", 0) == 0) {
            std::string payload = line.substr(4);
            if (payload.rfind("LOG|", 0) == 0) {
                size_t p1 = payload.find('|', 4);
                if (p1 != std::string::npos) {
                    std::string level = payload.substr(4, p1 - 4);
                    std::string msg = payload.substr(p1 + 1);
                    if (level == "WARN") {
                        DragonbornVoiceControl::LogWarn(msg);
                    } else if (level == "DEBUG") {
                        DragonbornVoiceControl::LogDebug(msg);
                    } else {
                        DragonbornVoiceControl::LogInfo(msg);
                    }
                }
                continue;
            }

            PipeResponse resp;
            resp.type = "DBG";
            resp.trigText = payload;

            {
                std::lock_guard lg(_respMutex);
                _responses.push_back(resp);
                if (_responses.size() > 128) {
                    _responses.pop_front();
                }
            }
        }
    }
}

void PipeClient::ThreadMain()
{
    DragonbornVoiceControl::LogDebug("[DVC_SERVER] thread started");

    while (_running.load())
    {
        if (!_connected.load())
        {
            HANDLE pipe = CreateFileW(
                PIPE_NAME,
                GENERIC_READ | GENERIC_WRITE,
                0,
                nullptr,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                nullptr
            );

            if (pipe != INVALID_HANDLE_VALUE) {
                _pipe = (void*)pipe;
                _connected = true;

                {
                    std::lock_guard lg(_connMutex);
                    _connEvent = true;
                }

                DragonbornVoiceControl::LogInfo("[DVC_SERVER] connected");
            } else {
                std::this_thread::sleep_for(std::chrono::milliseconds(1000));
                continue;
            }
        }

        bool wroteAny = false;
        {
            std::lock_guard lg(_sendMutex);

            if (_desiredGameLang.has_value()) {
                const std::string& desired = _desiredGameLang.value();
                if (!_lastSentGameLang.has_value() || _lastSentGameLang.value() != desired) {
                    if (!WriteLine(std::string("LANG|") + desired)) {
                        goto disconnect;
                    }
                    _lastSentGameLang = desired;
                    wroteAny = true;
                }
            }

            if (_pendingOptions.has_value()) {
                auto opts = _pendingOptions.value();
                _pendingOptions.reset();

                if (!WriteLine("OPEN|" + std::to_string(opts.size()))) {
                    goto disconnect;
                }

                for (auto& s : opts) {
                    std::string line = "OPT|" + s;
                    SanitizeInPlace(line);
                    if (!WriteLine(line)) {
                        goto disconnect;
                    }
                }

                if (!WriteLine("END")) {
                    goto disconnect;
                }

                wroteAny = true;
            }

            if (_pendingClose) {
                if (!WriteLine("CLOSE")) {
                    goto disconnect;
                }
                _pendingClose = false;
                wroteAny = true;
            }

            if (_pendingFavorites.has_value()) {
                auto lines = std::move(_pendingFavorites.value());
                _pendingFavorites.reset();

                for (const auto& line : lines) {
                    if (!WriteLine(line)) {
                        goto disconnect;
                    }
                }

                wroteAny = true;
            }

            if (_desiredCfgOpen.has_value()) {
                bool desired = _desiredCfgOpen.value();
                if (!_lastSentCfgOpen.has_value() || _lastSentCfgOpen.value() != desired) {
                    if (!WriteLine(std::string("CFG|OPEN|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgOpen = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgClose.has_value()) {
                bool desired = _desiredCfgClose.value();
                if (!_lastSentCfgClose.has_value() || _lastSentCfgClose.value() != desired) {
                    if (!WriteLine(std::string("CFG|CLOSE|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgClose = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgVoiceHandle.has_value()) {
                bool desired = _desiredCfgVoiceHandle.value();
                if (!_lastSentCfgVoiceHandle.has_value() || _lastSentCfgVoiceHandle.value() != desired) {
                    if (!WriteLine(std::string("CFG|SHOUTS|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgVoiceHandle = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgDebug.has_value()) {
                bool desired = _desiredCfgDebug.value();
                if (!_lastSentCfgDebug.has_value() || _lastSentCfgDebug.value() != desired) {
                    if (!WriteLine(std::string("CFG|DEBUG|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgDebug = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgSaveWav.has_value()) {
                bool desired = _desiredCfgSaveWav.value();
                if (!_lastSentCfgSaveWav.has_value() || _lastSentCfgSaveWav.value() != desired) {
                    if (!WriteLine(std::string("CFG|SAVE_WAV|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgSaveWav = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgDialogueSelect.has_value()) {
                bool desired = _desiredCfgDialogueSelect.value();
                if (!_lastSentCfgDialogueSelect.has_value() || _lastSentCfgDialogueSelect.value() != desired) {
                    if (!WriteLine(std::string("CFG|DIALOGUE_SELECT|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgDialogueSelect = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgWeapons.has_value()) {
                bool desired = _desiredCfgWeapons.value();
                if (!_lastSentCfgWeapons.has_value() || _lastSentCfgWeapons.value() != desired) {
                    if (!WriteLine(std::string("CFG|WEAPONS|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgWeapons = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgSpells.has_value()) {
                bool desired = _desiredCfgSpells.value();
                if (!_lastSentCfgSpells.has_value() || _lastSentCfgSpells.value() != desired) {
                    if (!WriteLine(std::string("CFG|SPELLS|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgSpells = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgPowers.has_value()) {
                bool desired = _desiredCfgPowers.value();
                if (!_lastSentCfgPowers.has_value() || _lastSentCfgPowers.value() != desired) {
                    if (!WriteLine(std::string("CFG|POWERS|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgPowers = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgPotions.has_value()) {
                bool desired = _desiredCfgPotions.value();
                if (!_lastSentCfgPotions.has_value() || _lastSentCfgPotions.value() != desired) {
                    if (!WriteLine(std::string("CFG|POTIONS|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgPotions = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgPotionsQuickUse.has_value()) {
                bool desired = _desiredCfgPotionsQuickUse.value();
                if (!_lastSentCfgPotionsQuickUse.has_value() || _lastSentCfgPotionsQuickUse.value() != desired) {
                    if (!WriteLine(std::string("CFG|POTIONS_QUICK_USE|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgPotionsQuickUse = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgPotionsBestPotion.has_value()) {
                bool desired = _desiredCfgPotionsBestPotion.value();
                if (!_lastSentCfgPotionsBestPotion.has_value() || _lastSentCfgPotionsBestPotion.value() != desired) {
                    if (!WriteLine(std::string("CFG|POTIONS_BEST_POTION|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgPotionsBestPotion = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgSpecifyHand.has_value()) {
                bool desired = _desiredCfgSpecifyHand.value();
                if (!_lastSentCfgSpecifyHand.has_value() || _lastSentCfgSpecifyHand.value() != desired) {
                    if (!WriteLine(std::string("CFG|SPECIFY_HAND|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgSpecifyHand = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgQuickEquip.has_value()) {
                bool desired = _desiredCfgQuickEquip.value();
                if (!_lastSentCfgQuickEquip.has_value() || _lastSentCfgQuickEquip.value() != desired) {
                    if (!WriteLine(std::string("CFG|QUICK_EQUIP|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgQuickEquip = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgKeyConsole.has_value()) {
                bool desired = _desiredCfgKeyConsole.value();
                if (!_lastSentCfgKeyConsole.has_value() || _lastSentCfgKeyConsole.value() != desired) {
                    if (!WriteLine(std::string("CFG|KEY_CONSOLE|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgKeyConsole = desired;
                    wroteAny = true;
                }
            }

            if (_desiredCfgPauseResume.has_value()) {
                bool desired = _desiredCfgPauseResume.value();
                if (!_lastSentCfgPauseResume.has_value() || _lastSentCfgPauseResume.value() != desired) {
                    if (!WriteLine(std::string("CFG|PAUSE_RESUME|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentCfgPauseResume = desired;
                    wroteAny = true;
                }
            }

            if (_desiredShoutContext.has_value() &&
                _desiredPlayerDrawn.has_value() &&
                _desiredPlayerCombat.has_value()) {
                const bool desiredShoutContext = _desiredShoutContext.value();
                const bool desiredPlayerDrawn = _desiredPlayerDrawn.value();
                const bool desiredPlayerCombat = _desiredPlayerCombat.value();
                const bool desiredBlockingMenu = _desiredBlockingMenu.value_or(false);
                const bool stateChanged =
                    !_lastSentShoutContext.has_value() ||
                    !_lastSentPlayerDrawn.has_value() ||
                    !_lastSentPlayerCombat.has_value() ||
                    !_lastSentBlockingMenu.has_value() ||
                    _lastSentShoutContext.value() != desiredShoutContext ||
                    _lastSentPlayerDrawn.value() != desiredPlayerDrawn ||
                    _lastSentPlayerCombat.value() != desiredPlayerCombat ||
                    _lastSentBlockingMenu.value() != desiredBlockingMenu;

                if (stateChanged) {
                    std::string line = "STATE|ALL|";
                    line += desiredShoutContext ? "1" : "0";
                    line += "|";
                    line += desiredPlayerDrawn ? "1" : "0";
                    line += "|";
                    line += desiredPlayerCombat ? "1" : "0";
                    line += "|";
                    line += desiredBlockingMenu ? "1" : "0";
                    if (!WriteLine(line)) {
                        goto disconnect;
                    }
                    _lastSentShoutContext = desiredShoutContext;
                    _lastSentPlayerDrawn = desiredPlayerDrawn;
                    _lastSentPlayerCombat = desiredPlayerCombat;
                    _lastSentBlockingMenu = desiredBlockingMenu;
                    wroteAny = true;
                }
            }

            if (_pendingListen.has_value()) {
                bool on = _pendingListen.value();
                if (!WriteLine(std::string("LISTEN|") + (on ? "1" : "0"))) {
                    goto disconnect;
                }
                _pendingListen.reset();
                wroteAny = true;
            }


        }

        ProcessIncoming();

        if (!wroteAny) {
            std::this_thread::sleep_for(std::chrono::milliseconds(15));
        }

        continue;

    disconnect:
        HandleDisconnect();
        std::this_thread::sleep_for(std::chrono::milliseconds(1000));
    }
}

void PipeClient::HandleDisconnect()
{
    if (!_connected && !_pipe) {
        return;
    }

    DragonbornVoiceControl::LogWarn("[DVC_SERVER] disconnect");

    if (_pipe) {
        CloseHandle((HANDLE)_pipe);
        _pipe = nullptr;
    }
    _connected = false;
    _clientReady = false;

    // Force sticky CFG re-send after next reconnect.
    // Server process may have restarted and lost runtime state.
    {
        std::lock_guard lg(_sendMutex);
        _lastSentCfgOpen.reset();
        _lastSentCfgClose.reset();
        _lastSentCfgVoiceHandle.reset();
        _lastSentCfgDebug.reset();
        _lastSentCfgSaveWav.reset();
        _lastSentCfgDialogueSelect.reset();
        _lastSentCfgWeapons.reset();
        _lastSentCfgSpells.reset();
        _lastSentCfgPowers.reset();
        _lastSentCfgPotions.reset();
        _lastSentCfgPotionsQuickUse.reset();
        _lastSentCfgPotionsBestPotion.reset();
        _lastSentCfgSpecifyHand.reset();
        _lastSentCfgQuickEquip.reset();
        _lastSentCfgKeyConsole.reset();
        _lastSentCfgPauseResume.reset();
        _lastSentShoutContext.reset();
        _lastSentPlayerDrawn.reset();
        _lastSentPlayerCombat.reset();
        _lastSentBlockingMenu.reset();
        _lastSentGameLang.reset();
    }

    {
        std::lock_guard lg(_connMutex);
        _connEvent = false;
        _readyEvent = false;
    }
}
