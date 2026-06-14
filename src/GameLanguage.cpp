#include "PCH.h"

#include "GameLanguage.h"

#include <algorithm>
#include <cctype>
#include <string>

namespace
{
    std::string TrimLower(const std::string& raw)
    {
        auto is_space = [](unsigned char ch) { return std::isspace(ch) != 0; };
        std::string s = raw;
        s.erase(s.begin(), std::find_if(s.begin(), s.end(), [&](unsigned char ch) { return !is_space(ch); }));
        s.erase(std::find_if(s.rbegin(), s.rend(), [&](unsigned char ch) { return !is_space(ch); }).base(), s.end());
        std::transform(s.begin(), s.end(), s.begin(), [](unsigned char ch) {
            return static_cast<char>(std::tolower(ch));
        });
        return s;
    }

    std::string NormalizeKey(const std::string& raw)
    {
        std::string s = TrimLower(raw);
        std::string out;
        out.reserve(s.size());
        for (unsigned char ch : s) {
            if (std::isalnum(ch)) {
                out.push_back(static_cast<char>(ch));
            }
        }
        return out;
    }
}

namespace DragonbornVoiceControl
{
    GameLanguageInfo DetectGameLanguage()
    {
        GameLanguageInfo info;

        std::string raw;
        auto ini = RE::INISettingCollection::GetSingleton();
        if (ini) {
            if (auto setting = ini->GetSetting("sLanguage:General")) {
                const char* s = setting->GetString();
                if (s && *s) {
                    raw = s;
                }
            }
        }

        if (raw.empty()) {
            return info;
        }

        info.raw = raw;
        const std::string key = NormalizeKey(info.raw);

        if (key == "en" || key == "english") {
            info.code = "en";
            info.label = "english";
        } else if (key == "ru" || key == "russian") {
            info.code = "ru";
            info.label = "russian";
        } else if (key == "fr" || key == "french") {
            info.code = "fr";
            info.label = "french";
        } else if (key == "it" || key == "italian") {
            info.code = "it";
            info.label = "italian";
        } else if (key == "de" || key == "german" || key == "deutsch") {
            info.code = "de";
            info.label = "german";
        } else if (key == "es" || key == "spanish" || key == "espanol") {
            info.code = "es";
            info.label = "spanish";
        } else if (key == "pl" || key == "polish" || key == "polski") {
            info.code = "pl";
            info.label = "polish";
        } else if (key == "ja" || key == "japanese") {
            info.code = "ja";
            info.label = "japanese";
        } else if (key == "cn" || key == "zh" || key == "zhcn" || key == "zhhant" ||
                   key == "chinese" || key == "chinesetraditional" || key == "traditionalchinese") {
            info.code = "cn";
            info.label = "traditional_chinese";
        }

        return info;
    }
}
