#pragma once

#include <string>

namespace DragonbornVoiceControl
{
    void RegisterDialogueWatcher();
    void LogOptionsIfChanged(const char* tag);
    void RequestSelectIndex_MainThread(int index0, const std::string& text, float score);
    bool IsDialogueOpen();
}
