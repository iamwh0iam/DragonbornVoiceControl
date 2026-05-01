#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <Windows.h>

#ifdef GetObject
#undef GetObject
#endif

#include <chrono>
#include <cstring>
#include <cstdint>
#include <string>

namespace DragonbornVoiceControl
{
    inline double GetNowSec()
    {
        using namespace std::chrono;
        return duration_cast<duration<double>>(steady_clock::now().time_since_epoch()).count();
    }

    namespace detail
    {
        inline bool IsLikelyBadPointer(const void* ptr)
        {
            const auto value = reinterpret_cast<std::uintptr_t>(ptr);
            return value == 0 ||
                   value < 0x10000ULL ||
                   value == static_cast<std::uintptr_t>(-1) ||
                   value == static_cast<std::uintptr_t>(-2) ||
                   value >= 0x0000FFFFFFFF0000ULL;
        }

        inline bool IsLikelyReadablePointer(const void* ptr)
        {
            if (IsLikelyBadPointer(ptr)) {
                return false;
            }

            MEMORY_BASIC_INFORMATION mbi{};
            if (VirtualQuery(ptr, &mbi, sizeof(mbi)) != sizeof(mbi)) {
                return false;
            }

            if (mbi.State != MEM_COMMIT) {
                return false;
            }

            if ((mbi.Protect & PAGE_GUARD) != 0 || (mbi.Protect & PAGE_NOACCESS) != 0) {
                return false;
            }

            constexpr DWORD kReadableProtect =
                PAGE_READONLY |
                PAGE_READWRITE |
                PAGE_WRITECOPY |
                PAGE_EXECUTE_READ |
                PAGE_EXECUTE_READWRITE |
                PAGE_EXECUTE_WRITECOPY;

            return (mbi.Protect & kReadableProtect) != 0;
        }

        inline const char* SafeGameCString(const char* raw, std::size_t maxLen = 512)
        {
            if (!IsLikelyReadablePointer(raw)) {
                return nullptr;
            }

#pragma warning(push)
#pragma warning(disable: 4996)
            if (IsBadStringPtrA(raw, maxLen)) {
                return nullptr;
            }
#pragma warning(pop)

            const auto len = strnlen_s(raw, maxLen);
            if (len == 0 || len >= maxLen) {
                return nullptr;
            }

            return raw;
        }

        inline std::string SafeGameString(const char* raw, std::size_t maxLen = 512)
        {
            raw = SafeGameCString(raw, maxLen);
            if (!raw) {
                return {};
            }

            std::string out(raw);
            for (char& c : out) {
                const auto uc = static_cast<unsigned char>(c);
                if (c == '|' || c == '\n' || c == '\r' || (uc < 0x20 && c != '\t')) {
                    c = ' ';
                }
            }
            return out;
        }
    }
}
