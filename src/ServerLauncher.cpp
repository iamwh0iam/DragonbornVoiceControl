#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <Shlwapi.h>
#include "ServerLauncher.h"
#include <cwctype>
#include <cstdarg>
#include <cwchar>
#include <string>
#include <thread>
#include <utility>
#include <vector>

static std::wstring DirOf(const std::wstring& path) {
    wchar_t tmp[MAX_PATH]{};
    wcsncpy_s(tmp, path.c_str(), _TRUNCATE);
    PathRemoveFileSpecW(tmp);
    return tmp;
}

static std::wstring JoinPath(const std::wstring& a, const std::wstring& b) {
    if (a.empty()) return b;
    if (b.empty()) return a;
    if (a.back() == L'\\' || a.back() == L'/') return a + b;
    return a + L"\\" + b;
}

static std::wstring PickFirstExisting(const std::vector<std::wstring>& cands) {
    for (const auto& p : cands) {
        if (!p.empty() && PathFileExistsW(p.c_str())) return p;
    }
    return L"";
}

static std::wstring ResolveRealPathByHandle(const std::wstring& maybeVirtualPath) {
    HANDLE h = CreateFileW(maybeVirtualPath.c_str(), 0,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return L"";
    wchar_t buf[MAX_PATH]{};
    DWORD n = GetFinalPathNameByHandleW(h, buf, MAX_PATH, FILE_NAME_NORMALIZED);
    CloseHandle(h);
    if (!n || n >= MAX_PATH) return L"";
    std::wstring p(buf);
    const std::wstring prefix = L"\\\\?\\";
    if (p.rfind(prefix, 0) == 0) p = p.substr(prefix.size());
    return p;
}

static void RotateLaunchLogs(const std::wstring& runtimeDir)
{
    const std::wstring log00 = JoinPath(runtimeDir, L"dvc_server_launch00.log");
    const std::wstring log01 = JoinPath(runtimeDir, L"dvc_server_launch01.log");

    DeleteFileW(log01.c_str());
    MoveFileExW(log00.c_str(), log01.c_str(), MOVEFILE_REPLACE_EXISTING);
}

static void AppendLaunchLog(std::vector<std::wstring>& out, const wchar_t* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    va_list apCopy;
    va_copy(apCopy, ap);
    int len = _vscwprintf(fmt, ap);
    if (len >= 0) {
        std::wstring line;
        line.resize(static_cast<size_t>(len));
        vswprintf_s(line.data(), line.size() + 1, fmt, apCopy);
        out.push_back(line);
    }
    va_end(apCopy);
    va_end(ap);
}

static void FlushLaunchLogs(const std::wstring& runtimeDir, const std::vector<std::wstring>& lines) {
    if (runtimeDir.empty() || lines.empty()) return;
    RotateLaunchLogs(runtimeDir);
    std::wstring logPath = JoinPath(runtimeDir, L"dvc_server_launch00.log");
    FILE* f = nullptr;
    _wfopen_s(&f, logPath.c_str(), L"w, ccs=UTF-8");
    if (!f) return;
    for (const auto& line : lines) {
        fwprintf(f, L"%s\n", line.c_str());
    }
    fclose(f);
}

static void StartExitLogMonitor(HANDLE procHandle,
                                DWORD pid,
                                const std::wstring& runtimeDir,
                                const std::vector<std::wstring>& baseLogs)
{
    HANDLE procDup = nullptr;
    if (!DuplicateHandle(GetCurrentProcess(), procHandle, GetCurrentProcess(), &procDup, 0, FALSE, DUPLICATE_SAME_ACCESS)) {
        return;
    }

    std::vector<std::wstring> logs = baseLogs;
    std::thread([procDup, pid, runtimeDir, logs = std::move(logs)]() mutable {
        DWORD exitCode = 0;
        WaitForSingleObject(procDup, INFINITE);
        if (GetExitCodeProcess(procDup, &exitCode)) {
            if (exitCode != 0) {
                AppendLaunchLog(logs, L"PROCESS EXIT pid=%u code=%u", pid, exitCode);
                FlushLaunchLogs(runtimeDir, logs);
            }
        } else {
            AppendLaunchLog(logs, L"PROCESS EXIT pid=%u (GetExitCodeProcess failed)", pid);
            FlushLaunchLogs(runtimeDir, logs);
        }
        CloseHandle(procDup);
    }).detach();
}

static bool EnvKeyEqualsNoCase(const wchar_t* entry, const wchar_t* key)
{
    const wchar_t* p = entry;
    const wchar_t* k = key;
    while (*p && *p != L'=' && *k) {
        wchar_t pc = (wchar_t)towupper(*p);
        wchar_t kc = (wchar_t)towupper(*k);
        if (pc != kc) {
            return false;
        }
        ++p;
        ++k;
    }
    return (*k == 0) && (*p == L'=');
}

static bool EnvKeyStartsWithNoCase(const wchar_t* entry, const wchar_t* prefix)
{
    const wchar_t* p = entry;
    const wchar_t* k = prefix;
    while (*p && *p != L'=' && *k) {
        wchar_t pc = (wchar_t)towupper(*p);
        wchar_t kc = (wchar_t)towupper(*k);
        if (pc != kc) {
            return false;
        }
        ++p;
        ++k;
    }
    return (*k == 0) && (*p == L'=' || *p == 0);
}

static bool EnvKeyContainsTokenNoCase(const wchar_t* entry, const wchar_t* token)
{
    if (!entry || !token) return false;
    std::wstring key;
    for (const wchar_t* p = entry; *p && *p != L'='; ++p) {
        key.push_back((wchar_t)towupper(*p));
    }
    std::wstring tok;
    for (const wchar_t* p = token; *p; ++p) {
        tok.push_back((wchar_t)towupper(*p));
    }
    return key.find(tok) != std::wstring::npos;
}

static bool IsVfsEnvVar(const wchar_t* entry)
{
    return EnvKeyStartsWithNoCase(entry, L"USVFS") ||
           EnvKeyStartsWithNoCase(entry, L"MO2") ||
           EnvKeyStartsWithNoCase(entry, L"MODORGANIZER") ||
           EnvKeyContainsTokenNoCase(entry, L"USVFS") ||
           EnvKeyContainsTokenNoCase(entry, L"MO2") ||
           EnvKeyContainsTokenNoCase(entry, L"VFS");
}

static void AppendEnvVar(std::vector<wchar_t>& out, const std::wstring& key, const std::wstring& value)
{
    const std::wstring line = key + L"=" + value;
    out.insert(out.end(), line.begin(), line.end());
    out.push_back(L'\0');
}

static std::vector<wchar_t> BuildEnvBlock_IsolatedPython(const std::wstring& realPyDir)
{
    std::vector<wchar_t> out;
    out.reserve(32768);

    LPWCH envs = GetEnvironmentStringsW();
    if (envs) {
        for (const wchar_t* p = envs; *p; p += wcslen(p) + 1) {
            if (EnvKeyEqualsNoCase(p, L"PYTHONHOME") ||
                EnvKeyEqualsNoCase(p, L"PYTHONPATH") ||
                EnvKeyEqualsNoCase(p, L"PYTHONNOUSERSITE")) {
                continue;
            }

            const size_t len = wcslen(p);
            out.insert(out.end(), p, p + len);
            out.push_back(L'\0');
        }
        FreeEnvironmentStringsW(envs);
    }

    AppendEnvVar(out, L"PYTHONHOME", realPyDir);

    const std::wstring lib  = JoinPath(realPyDir, L"Lib");
    const std::wstring site = JoinPath(lib,      L"site-packages");
    AppendEnvVar(out, L"PYTHONPATH", lib + L";" + site);

    AppendEnvVar(out, L"PYTHONNOUSERSITE", L"1");

    out.push_back(L'\0');
    return out;
}

static std::vector<wchar_t> BuildEnvBlock_SanitizedForPyInstaller()
{
    std::vector<wchar_t> out;
    out.reserve(32768);

    LPWCH envs = GetEnvironmentStringsW();
    if (envs) {
        for (const wchar_t* p = envs; *p; p += wcslen(p) + 1) {
            if (EnvKeyEqualsNoCase(p, L"PYTHONHOME") ||
                EnvKeyEqualsNoCase(p, L"PYTHONPATH") ||
                EnvKeyEqualsNoCase(p, L"PYTHONNOUSERSITE")) {
                continue;
            }

            const size_t len = wcslen(p);
            out.insert(out.end(), p, p + len);
            out.push_back(L'\0');
        }
        FreeEnvironmentStringsW(envs);
    }

    out.push_back(L'\0');
    return out;
}

static std::vector<wchar_t> BuildEnvBlock_ForRuntimeExe(const std::wstring& runtimeDir, const std::wstring& logDir)
{
    std::vector<wchar_t> out;
    out.reserve(32768);

    LPWCH envs = GetEnvironmentStringsW();
    if (envs) {
        for (const wchar_t* p = envs; *p; p += wcslen(p) + 1) {
            if (EnvKeyEqualsNoCase(p, L"PYTHONHOME") ||
                EnvKeyEqualsNoCase(p, L"PYTHONPATH") ||
                EnvKeyEqualsNoCase(p, L"PYTHONNOUSERSITE")) {
                continue;
            }

            if (IsVfsEnvVar(p)) {
                continue;
            }

            const size_t len = wcslen(p);
            out.insert(out.end(), p, p + len);
            out.push_back(L'\0');
        }
        FreeEnvironmentStringsW(envs);
    }

    const std::wstring internalDir = JoinPath(runtimeDir, L"_internal");
    const std::wstring zipPath = JoinPath(internalDir, L"base_library.zip");
    const std::wstring dynloadDir = JoinPath(internalDir, L"python3.12\\lib-dynload");

    AppendEnvVar(out, L"PYTHONHOME", internalDir);
    AppendEnvVar(out, L"PYTHONPATH", zipPath + L";" + dynloadDir + L";" + internalDir);
    AppendEnvVar(out, L"PYTHONNOUSERSITE", L"1");

    out.push_back(L'\0');
    return out;
}

ServerLauncher& ServerLauncher::Get() {
    static ServerLauncher inst;
    return inst;
}

bool ServerLauncher::StartFromIni(const std::wstring& dataDir, const std::wstring& iniPath) {
    if (_procHandle) return true;

    std::vector<std::wstring> launchLogs;

    const std::wstring iniDir = DirOf(iniPath);
    std::wstring gameRoot = DirOf(dataDir);

    const std::wstring resolvedIni = ResolveRealPathByHandle(iniPath);
    const std::wstring realIni = !resolvedIni.empty() ? resolvedIni : iniPath;

    // app.zip belongs to the main mod and must be resolved through MO2's virtual Data path,
    // independently from the runtime exe real path.
    const std::wstring appPick = JoinPath(JoinPath(iniDir, L"DVCRuntime"), L"app.zip");
    const std::wstring resolvedApp = ResolveRealPathByHandle(appPick);
    const std::wstring realApp = !resolvedApp.empty() ? resolvedApp : appPick;

    std::vector<std::wstring> exeCands = {
        JoinPath(JoinPath(gameRoot, L"mods"), L"Dragonborn Voice Control\\DVCRuntime\\DVCRuntime.exe"),
        JoinPath(JoinPath(gameRoot, L"mods"), L"Dragonborn Voice Control\\DVCRuntime\\DragonbornVoiceControlServer.exe"),
        JoinPath(JoinPath(gameRoot, L"mods"), L"Dragonborn Voice Control\\DVCRuntime\\DragonbornVoiceControlServer\\DragonbornVoiceControlServer.exe"),

        JoinPath(JoinPath(gameRoot, L"mods"), L"DVCRuntime\\DVCRuntime.exe"),
        JoinPath(JoinPath(gameRoot, L"mods"), L"DVCRuntime\\DragonbornVoiceControlServer.exe"),
        JoinPath(JoinPath(gameRoot, L"mods"), L"DVCRuntime\\DragonbornVoiceControlServer\\DragonbornVoiceControlServer.exe"),

        JoinPath(dataDir, L"DVCRuntime\\DVCRuntime.exe"),
        JoinPath(dataDir, L"DVCRuntime\\DragonbornVoiceControlServer.exe"),
        JoinPath(dataDir, L"DVCRuntime\\DragonbornVoiceControlServer\\DragonbornVoiceControlServer.exe"),

        JoinPath(iniDir, L"DVCRuntime\\DVCRuntime.exe"),
        JoinPath(iniDir, L"DVCRuntime\\DragonbornVoiceControlServer.exe"),
    };

    std::vector<std::wstring> cands = {
        JoinPath(JoinPath(gameRoot, L"mods"), L"Dragonborn Voice Control\\DVCRuntime\\python312\\python.exe"),

        JoinPath(JoinPath(gameRoot, L"mods"), L"DVCRuntime\\python312\\python.exe"),

        JoinPath(iniDir, L"DVCRuntime\\python312\\python.exe"),
        JoinPath(iniDir, L"python312\\python.exe"),

        JoinPath(dataDir, L"DVCRuntime\\python312\\python.exe"),
    };

    std::wstring exe = PickFirstExisting(exeCands);
    std::wstring resolvedExe = !exe.empty() ? ResolveRealPathByHandle(exe) : L"";
    std::wstring realExe = (!resolvedExe.empty() ? resolvedExe : exe);

    std::wstring runtimeDir;

    if (!realExe.empty()) {
        runtimeDir = DirOf(realExe);
    } else if (!exe.empty()) {
        runtimeDir = DirOf(exe);
    }

    if (runtimeDir.empty()) {
        std::wstring python = PickFirstExisting(cands);
        if (python.empty()) {
            AppendLaunchLog(launchLogs, L"LAUNCH FAIL: runtime exe/python.exe not found");
            FlushLaunchLogs(iniDir, launchLogs);
            return false;
        }

        std::wstring resolved = ResolveRealPathByHandle(python);
        std::wstring realPython = !resolved.empty() ? resolved : python;

        std::wstring realPyDir = DirOf(realPython);
        runtimeDir = DirOf(realPyDir);
        std::wstring script = JoinPath(runtimeDir, L"bootstrap.py");
        if (!PathFileExistsW(script.c_str())) {
            script = JoinPath(runtimeDir, L"main.py");
        }

        AppendLaunchLog(launchLogs, L"LAUNCH AUTODETECT: dataDir=%s iniPath=%s", dataDir.c_str(), iniPath.c_str());
        AppendLaunchLog(launchLogs, L" INI_PICK: %s", iniPath.c_str());
        AppendLaunchLog(launchLogs, L" INI_REAL: %s", realIni.c_str());
        AppendLaunchLog(launchLogs, L" APP_PICK: %s", appPick.c_str());
        AppendLaunchLog(launchLogs, L" APP_REAL: %s", realApp.c_str());
        AppendLaunchLog(launchLogs, L" MODE: PY");
        AppendLaunchLog(launchLogs, L" PY_PICK: %s", python.c_str());
        AppendLaunchLog(launchLogs, L" PY_REAL: %s", realPython.c_str());

        std::vector<wchar_t> envBuf = BuildEnvBlock_IsolatedPython(realPyDir);

        AppendLaunchLog(launchLogs, L"LAUNCH ATTEMPT (PY): app=%s script=%s", realPython.c_str(), script.c_str());

        std::wstring cmd = L"\"" + realPython + L"\" -u \"" + script + L"\" --ini \"" + realIni + L"\" --app \"" + realApp + L"\"";
        std::vector<wchar_t> cmdBuf(cmd.begin(), cmd.end());
        cmdBuf.push_back(L'\0');

        STARTUPINFOW si{};
        si.cb = sizeof(si);
        PROCESS_INFORMATION pi{};

        DWORD flags = CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_PROCESS_GROUP | CREATE_NEW_CONSOLE;
        BOOL ok = CreateProcessW(realPython.c_str(), cmdBuf.data(), nullptr, nullptr, FALSE, flags,
                                 envBuf.empty() ? nullptr : envBuf.data(), runtimeDir.c_str(), &si, &pi);
        if (!ok) {
            DWORD gle = GetLastError();
            AppendLaunchLog(launchLogs, L"LAUNCH FAILED (PY): gle=%u", gle);
            FlushLaunchLogs(runtimeDir, launchLogs);
            return false;
        }

        _procHandle = pi.hProcess;
        _procId = pi.dwProcessId;
        CloseHandle(pi.hThread);

        AppendLaunchLog(launchLogs, L"LAUNCH PID=%u", _procId);

        DWORD r = WaitForSingleObject(_procHandle, 250);
        if (r == WAIT_OBJECT_0) {
            DWORD exitCode = 0;
            if (GetExitCodeProcess(_procHandle, &exitCode)) {
                AppendLaunchLog(launchLogs, L"LAUNCH ENDED IMMEDIATELY: exit=%u", exitCode);
                if (exitCode != 0) {
                    FlushLaunchLogs(runtimeDir, launchLogs);
                }
            } else {
                AppendLaunchLog(launchLogs, L"LAUNCH ENDED IMMEDIATELY: (GetExitCodeProcess failed)");
                FlushLaunchLogs(runtimeDir, launchLogs);
            }
            return true;
        }

        StartExitLogMonitor(_procHandle, _procId, runtimeDir, launchLogs);

        return true;
    }

    AppendLaunchLog(launchLogs, L"LAUNCH AUTODETECT: dataDir=%s iniPath=%s", dataDir.c_str(), iniPath.c_str());
    AppendLaunchLog(launchLogs, L" INI_PICK: %s", iniPath.c_str());
    AppendLaunchLog(launchLogs, L" INI_REAL: %s", realIni.c_str());
    AppendLaunchLog(launchLogs, L" APP_PICK: %s", appPick.c_str());
    AppendLaunchLog(launchLogs, L" APP_REAL: %s", realApp.c_str());
    AppendLaunchLog(launchLogs, L" MODE: EXE");
    AppendLaunchLog(launchLogs, L" EXE_PICK: %s", exe.c_str());
    AppendLaunchLog(launchLogs, L" EXE_REAL: %s", realExe.c_str());

    const std::wstring pickDir = exe.empty() ? L"" : DirOf(exe);
    const std::wstring realDir = realExe.empty() ? L"" : DirOf(realExe);
    const std::wstring realInternal = realDir.empty() ? L"" : JoinPath(realDir, L"_internal");

    const std::wstring launchExe = !realExe.empty() ? realExe : exe;
    const std::wstring launchDir = !realDir.empty() ? realDir : pickDir;
    const std::wstring appHomeDir = !realDir.empty() ? realDir : pickDir;

    AppendLaunchLog(launchLogs, L"LAUNCH ATTEMPT (EXE): app=%s", launchExe.c_str());

    std::wstring cmd = L"\"" + launchExe + L"\" --ini \"" + realIni + L"\" --app \"" + realApp + L"\"";
    std::vector<wchar_t> cmdBuf(cmd.begin(), cmd.end());
    cmdBuf.push_back(L'\0');

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};

    std::vector<wchar_t> envBuf = BuildEnvBlock_ForRuntimeExe(!realDir.empty() ? realDir : pickDir, runtimeDir);
    if (!envBuf.empty() && envBuf.back() == L'\0') {
        envBuf.pop_back();
    }
    if (!appHomeDir.empty()) {
        AppendEnvVar(envBuf, L"PYI_APPLICATION_HOME_DIR", appHomeDir);
        AppendEnvVar(envBuf, L"_PYI_APPLICATION_HOME_DIR", appHomeDir);
    }
    AppendEnvVar(envBuf, L"DVC_ENV_SENTINEL", L"1");
    envBuf.push_back(L'\0');

    DWORD flags = CREATE_NEW_PROCESS_GROUP | CREATE_NEW_CONSOLE | CREATE_UNICODE_ENVIRONMENT;
    BOOL ok = CreateProcessW(launchExe.c_str(), cmdBuf.data(), nullptr, nullptr, FALSE, flags,
                             envBuf.empty() ? nullptr : envBuf.data(), launchDir.c_str(), &si, &pi);
    if (!ok) {
        DWORD gle = GetLastError();
        AppendLaunchLog(launchLogs, L"LAUNCH FAILED (EXE): gle=%u", gle);
        FlushLaunchLogs(runtimeDir, launchLogs);
        return false;
    }

    _procHandle = pi.hProcess;
    _procId = pi.dwProcessId;
    CloseHandle(pi.hThread);

    AppendLaunchLog(launchLogs, L"LAUNCH PID=%u", _procId);

    DWORD r = WaitForSingleObject(_procHandle, 250);
    if (r == WAIT_OBJECT_0) {
        DWORD exitCode = 0;
        if (GetExitCodeProcess(_procHandle, &exitCode)) {
            AppendLaunchLog(launchLogs, L"LAUNCH ENDED IMMEDIATELY: exit=%u", exitCode);
            if (exitCode != 0) {
                FlushLaunchLogs(runtimeDir, launchLogs);
            }
        } else {
            AppendLaunchLog(launchLogs, L"LAUNCH ENDED IMMEDIATELY: (GetExitCodeProcess failed)");
            FlushLaunchLogs(runtimeDir, launchLogs);
        }
        return true;
    }

    StartExitLogMonitor(_procHandle, _procId, runtimeDir, launchLogs);

    return true;
}

void ServerLauncher::Stop() {
    if (_procHandle) {
        TerminateProcess((HANDLE)_procHandle, 0);
        CloseHandle((HANDLE)_procHandle);
        _procHandle = nullptr;
    }
}
