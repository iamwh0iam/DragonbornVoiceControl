#pragma once

#include <string>

namespace DragonbornVoiceControl
{
    void SetupLogging(const SKSE::LoadInterface* skse);
    void LogDebug(const std::string& s);
    void LogInfo(const std::string& s);
    void LogWarn(const std::string& s);
    void LogError(const std::string& s);
    void LogLine(const std::string& s);
}
