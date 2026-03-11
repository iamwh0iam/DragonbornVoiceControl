#include "PipeClient.h"

#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
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

void PipeClient::SendListenCommands(bool on)
{
    std::lock_guard lg(_sendMutex);
    _pendingListenCommands = on;
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

void PipeClient::SendPotionsAllowed(const std::vector<ItemEntry>& potions)
{
    (void)potions;
}

void PipeClient::SendAllFavorites(
    const std::vector<ShoutEntry>& shouts,
    const std::vector<PowerEntry>& powers,
    const std::vector<ItemEntry>& weapons,
    const std::vector<ItemEntry>& spells,
    const std::vector<ItemEntry>& potions)
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
        lines.push_back("FAV|POTION|" + formId + "|" + name);
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

            SKSE::log::info("[DVC_SERVER] recv RES index={} score={}", resp.index, resp.score);
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
                                size_t p4 = line.find('|', p3 + 1);
                                if (p4 != std::string::npos) {
                                    resp.trigText = line.substr(p4 + 1);
                                }
                            }
                        }
                    } else if (resp.trigKind == "weapon" || resp.trigKind == "spell" || resp.trigKind == "potion") {
                        // TRIG|weapon|formid|score|text
                        size_t p2 = line.find('|', p1 + 1);
                        if (p2 != std::string::npos) {
                            resp.itemFormID = line.substr(p1 + 1, p2 - p1 - 1);
                            size_t p3 = line.find('|', p2 + 1);
                            if (p3 != std::string::npos) {
                                resp.score = std::stof(line.substr(p2 + 1, p3 - p2 - 1));
                                resp.trigText = line.substr(p3 + 1);
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
                SKSE::log::info("[DVC_SERVER] recv TRIG shout formid={} power={} score={} text={}",
                    resp.shoutFormID, resp.shoutPower, resp.score, resp.trigText);
            } else if (resp.trigKind == "power") {
                SKSE::log::info("[DVC_SERVER] recv TRIG power formid={} score={} text={}",
                    resp.powerFormID, resp.score, resp.trigText);
            } else if (resp.trigKind == "weapon" || resp.trigKind == "spell" || resp.trigKind == "potion") {
                SKSE::log::info("[DVC_SERVER] recv TRIG {} formid={} score={} text={}",
                    resp.trigKind, resp.itemFormID, resp.score, resp.trigText);
            } else {
                SKSE::log::info("[DVC_SERVER] recv TRIG kind={} score={} text={}", resp.trigKind, resp.score, resp.trigText);
            }
        } else if (line.rfind("DBG|", 0) == 0) {
            PipeResponse resp;
            resp.type = "DBG";
            resp.trigText = line.substr(4);

            {
                std::lock_guard lg(_respMutex);
                _responses.push_back(resp);
                if (_responses.size() > 128) {
                    _responses.pop_front();
                }
            }

            SKSE::log::info("[DVC_SERVER] recv DBG text={}", resp.trigText);
        } else {
            if (line.rfind("effective:", 0) == 0) {
                std::string payload = line.substr(std::string("effective:").size());
                if (!payload.empty() && payload.front() == ' ') {
                    payload.erase(0, 1);
                }
                SKSE::log::info("[DVC_SERVER] Listen status: {}", payload);
            } else {
                SKSE::log::info("[DVC_SERVER] recv: {}", line);
            }
        }
    }
}

void PipeClient::ThreadMain()
{
    SKSE::log::info("[DVC_SERVER] thread started");

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

                SKSE::log::info("[DVC_SERVER] connected");
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

            if (_pendingListen.has_value()) {
                bool on = _pendingListen.value();
                if (!WriteLine(std::string("LISTEN|") + (on ? "1" : "0"))) {
                    goto disconnect;
                }
                _pendingListen.reset();
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

            if (_desiredShoutContext.has_value()) {
                bool desired = _desiredShoutContext.value();
                if (!_lastSentShoutContext.has_value() || _lastSentShoutContext.value() != desired) {
                    if (!WriteLine(std::string("STATE|SHOUT_CONTEXT|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentShoutContext = desired;
                    wroteAny = true;
                }
            }

            if (_desiredPlayerDrawn.has_value()) {
                bool desired = _desiredPlayerDrawn.value();
                if (!_lastSentPlayerDrawn.has_value() || _lastSentPlayerDrawn.value() != desired) {
                    if (!WriteLine(std::string("STATE|DRAWN|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentPlayerDrawn = desired;
                    wroteAny = true;
                }
            }

            if (_desiredPlayerCombat.has_value()) {
                bool desired = _desiredPlayerCombat.value();
                if (!_lastSentPlayerCombat.has_value() || _lastSentPlayerCombat.value() != desired) {
                    if (!WriteLine(std::string("STATE|COMBAT|") + (desired ? "1" : "0"))) {
                        goto disconnect;
                    }
                    _lastSentPlayerCombat = desired;
                    wroteAny = true;
                }
            }

            // Important ordering:
            // server ignores LISTEN|SHOUTS|1 while CFG for voice commands is still 0.
            // Send CFG first, then LISTEN|SHOUTS to avoid losing the enable command.
            if (_pendingListenCommands.has_value()) {
                bool on = _pendingListenCommands.value();
                if (!WriteLine(std::string("LISTEN|SHOUTS|") + (on ? "1" : "0"))) {
                    goto disconnect;
                }
                _pendingListenCommands.reset();
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

    SKSE::log::info("[DVC_SERVER] disconnect");

    if (_pipe) {
        CloseHandle((HANDLE)_pipe);
        _pipe = nullptr;
    }
    _connected = false;

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
        _lastSentShoutContext.reset();
        _lastSentPlayerDrawn.reset();
        _lastSentPlayerCombat.reset();
        _lastSentGameLang.reset();
    }

    {
        std::lock_guard lg(_connMutex);
        _connEvent = false;
    }
}
