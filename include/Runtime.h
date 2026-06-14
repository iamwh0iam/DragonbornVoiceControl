#pragma once

namespace DragonbornVoiceControl
{
    void BeginSaveLoading();
    void RequestSaveReadySync(const char* label);
    void SendRuntimeConfig();
    void ForceRuntimeSync();
    void RefreshVoiceCommandState();
    void SyncShoutContextState();
    void RegisterMenuGateWatcher();
    bool IsBlockingMenuOpen();
    void StartPollThread();
    void StopPollThread();
}
