#include "PCH.h"

#include "Logging.h"

#include "Paths.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <optional>
#include <string>
#include <string_view>

#include <spdlog/sinks/basic_file_sink.h>
#include <spdlog/sinks/msvc_sink.h>
#include <spdlog/spdlog.h>

#include <ShlObj.h>
#include <Shlwapi.h>

namespace
{
    enum class LogLevel
    {
        Debug,
        Info,
        Warning
    };

    LogLevel g_logLevel = LogLevel::Debug;
    bool g_invalidLogLevel = false;

    std::string Trim(std::string_view input)
    {
        auto start = input.find_first_not_of(" \t\r\n");
        if (start == std::string_view::npos) {
            return {};
        }
        auto end = input.find_last_not_of(" \t\r\n");
        return std::string(input.substr(start, end - start + 1));
    }

    std::string ToLower(std::string_view input)
    {
        std::string result(input);
        std::transform(result.begin(), result.end(), result.begin(), [](unsigned char ch) {
            return static_cast<char>(std::tolower(ch));
        });
        return result;
    }

    std::optional<LogLevel> ParseLogLevel(std::string_view value)
    {
        const auto normalized = ToLower(Trim(value));
        if (normalized == "debug") {
            return LogLevel::Debug;
        }
        if (normalized == "info") {
            return LogLevel::Info;
        }
        if (normalized == "warning" || normalized == "warn") {
            return LogLevel::Warning;
        }
        return std::nullopt;
    }

    const char* LogLevelToString(LogLevel level)
    {
        switch (level) {
        case LogLevel::Debug:
            return "debug";
        case LogLevel::Info:
            return "info";
        case LogLevel::Warning:
            return "warning";
        default:
            return "debug";
        }
    }

    LogLevel ReadLogLevelFromIni()
    {
        g_invalidLogLevel = false;
        const std::filesystem::path iniPath = DragonbornVoiceControl::GetIniPathFromPlugin();
        std::ifstream file(iniPath);
        if (!file.is_open()) {
            return LogLevel::Debug;
        }

        std::string line;
        while (std::getline(file, line)) {
            const auto trimmed = Trim(line);
            if (trimmed.empty() || trimmed.front() == ';' || trimmed.front() == '#') {
                continue;
            }
            if (trimmed.front() == '[') {
                break;
            }

            const auto separator = trimmed.find('=');
            if (separator == std::string::npos) {
                continue;
            }

            const auto key = ToLower(Trim(trimmed.substr(0, separator)));
            if (key != "loglevel") {
                continue;
            }

            if (auto parsed = ParseLogLevel(trimmed.substr(separator + 1))) {
                return *parsed;
            }
            g_invalidLogLevel = true;
            return LogLevel::Debug;
        }

        return LogLevel::Debug;
    }

    bool ShouldLog(spdlog::level::level_enum level)
    {
        if (level >= spdlog::level::warn) {
            return true;
        }

        switch (g_logLevel) {
        case LogLevel::Debug:
            return level >= spdlog::level::debug;
        case LogLevel::Info:
            return level >= spdlog::level::info;
        case LogLevel::Warning:
            return level >= spdlog::level::warn;
        default:
            return level >= spdlog::level::debug;
        }
    }

    void LogAt(spdlog::level::level_enum level, const std::string& s)
    {
        if (ShouldLog(level)) {
            spdlog::log(level, "{}", s);
        }
    }

    spdlog::level::level_enum InferLevel(const std::string& s)
    {
        const auto upper = [&]() {
            std::string result(s);
            std::transform(result.begin(), result.end(), result.begin(), [](unsigned char ch) {
                return static_cast<char>(std::toupper(ch));
            });
            return result;
        }();

        if (upper.find("[ERR]") != std::string::npos ||
            upper.find("ERROR:") != std::string::npos ||
            upper.find("[FATAL]") != std::string::npos) {
            return spdlog::level::err;
        }
        if (upper.find("[WARN]") != std::string::npos ||
            upper.find("FAIL:") != std::string::npos ||
            upper.find("RESULT=FAIL") != std::string::npos) {
            return spdlog::level::warn;
        }
        if (upper.find("[DBG]") != std::string::npos) {
            return spdlog::level::debug;
        }
        return spdlog::level::info;
    }

    const char* GetRuntimeName()
    {
        if (REL::Module::IsVR()) {
            return "Skyrim VR";
        }
        if (REL::Module::IsAE()) {
            return "Skyrim AE";
        }
        return "Skyrim SE";
    }

    std::string FormatSKSEVersion(std::uint32_t rawVersion)
    {
        const auto major = static_cast<std::uint8_t>((rawVersion >> 24) & 0xFF);
        const auto minor = static_cast<std::uint8_t>((rawVersion >> 16) & 0xFF);
        const auto patch = static_cast<std::uint8_t>((rawVersion >> 8) & 0xFF);
        const auto build = static_cast<std::uint8_t>(rawVersion & 0xFF);

        return std::to_string(major) + "." + std::to_string(minor) + "." +
               std::to_string(patch) + "." + std::to_string(build);
    }
}

namespace DragonbornVoiceControl
{
    void SetupLogging(const SKSE::LoadInterface* skse)
    {
        auto buildDocsSksePath = [&]() -> std::optional<std::filesystem::path> {
            wchar_t exePath[MAX_PATH] = {};
            if (!GetModuleFileNameW(NULL, exePath, MAX_PATH)) {
                return std::nullopt;
            }

            const wchar_t* exeName = PathFindFileNameW(exePath);
            std::wstring edition = L"Skyrim";

            if (exeName) {
                if (_wcsicmp(exeName, L"SkyrimVR.exe") == 0) {
                    edition = L"Skyrim VR";
                } else if (_wcsicmp(exeName, L"SkyrimSE.exe") == 0 || _wcsicmp(exeName, L"SkyrimSELauncher.exe") == 0) {
                    edition = L"Skyrim Special Edition";
                } else if (_wcsicmp(exeName, L"TESV.exe") == 0 || _wcsicmp(exeName, L"Skyrim.exe") == 0) {
                    edition = L"Skyrim";
                }
            }

            wchar_t docs[MAX_PATH] = {};
            if (SUCCEEDED(SHGetFolderPathW(NULL, CSIDL_PERSONAL, NULL, 0, docs))) {
                std::filesystem::path p = docs;
                p /= "My Games";
                p /= edition;
                p /= "SKSE";

                std::error_code ec;
                if (std::filesystem::create_directories(p, ec) || std::filesystem::exists(p)) {
                    return p;
                }
            }

            return std::nullopt;
        };

        std::shared_ptr<spdlog::logger> log;
        std::optional<std::filesystem::path> logPath;

        if (auto docsPath = buildDocsSksePath()) {
            logPath = *docsPath / "DragonbornVoiceControl.log";
            auto sink = std::make_shared<spdlog::sinks::basic_file_sink_mt>(logPath->string(), true);
            log = std::make_shared<spdlog::logger>("global log", std::move(sink));
        } else if (auto logDir = SKSE::log::log_directory()) {
            logPath = *logDir / "DragonbornVoiceControl.log";
            auto sink = std::make_shared<spdlog::sinks::basic_file_sink_mt>(logPath->string(), true);
            log = std::make_shared<spdlog::logger>("global log", std::move(sink));
        } else {
            auto sink = std::make_shared<spdlog::sinks::msvc_sink_mt>();
            log = std::make_shared<spdlog::logger>("global log", std::move(sink));
        }

        spdlog::set_default_logger(std::move(log));
        spdlog::set_pattern("[%Y-%m-%d %H:%M:%S.%e] [%l] %v");
        spdlog::set_level(spdlog::level::debug);
        spdlog::flush_on(spdlog::level::debug);

        g_logLevel = ReadLogLevelFromIni();

        if (g_invalidLogLevel) {
            LogWarn("[INI][WARN] invalid LogLevel value; using debug");
        }
        LogDebug(std::string("LogLevel=") + LogLevelToString(g_logLevel));
        if (skse) {
            LogInfo(std::string(GetRuntimeName()) + " " + std::to_string(skse->RuntimeVersion()));
            LogInfo(std::string("SKSE ") + FormatSKSEVersion(skse->SKSEVersion()));
        }
    }

    void LogDebug(const std::string& s)
    {
        LogAt(spdlog::level::debug, s);
    }

    void LogInfo(const std::string& s)
    {
        LogAt(spdlog::level::info, s);
    }

    void LogWarn(const std::string& s)
    {
        LogAt(spdlog::level::warn, s);
    }

    void LogError(const std::string& s)
    {
        LogAt(spdlog::level::err, s);
    }

    void LogLine(const std::string& s)
    {
        LogAt(InferLevel(s), s);
    }
}
