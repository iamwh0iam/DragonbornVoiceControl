#include "PCH.h"

#include "Dialogue.h"

#include "Logging.h"
#include "PipeClient.h"
#include "Runtime.h"
#include "Settings.h"

#include <atomic>
#include <chrono>
#include <iomanip>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace DragonbornVoiceControl
{
    std::vector<std::string> g_lastOptions;
    static std::atomic_bool g_selectInFlight{ false };
    static std::atomic_bool g_dialogueOpen{ false };
    static std::atomic_bool g_dialogueVoiceSessionActive{ false };

    static std::string FormatScore(float score)
    {
        std::ostringstream out;
        out << std::fixed << std::setprecision(3) << score;
        return out.str();
    }

    static std::string Quote(const std::string& text)
    {
        std::string out = "\"";
        for (char ch : text) {
            if (ch == '"' || ch == '\\') {
                out.push_back('\\');
            }
            out.push_back(ch);
        }
        out.push_back('"');
        return out;
    }

    static bool DialogueVoiceFeaturesEnabled()
    {
        return IsDialogueSelectEnabled() || IsVoiceCloseEnabled();
    }

    static std::vector<std::string> SnapshotDialogueOptions()
    {
        std::vector<std::string> out;

        auto mtm = RE::MenuTopicManager::GetSingleton();
        if (!mtm || !mtm->dialogueList) {
            return out;
        }

        for (auto* dlg : *mtm->dialogueList) {
            if (!dlg) {
                continue;
            }
            const char* text = dlg->topicText.c_str();
            if (text && *text) {
                out.emplace_back(text);
            }
        }

        return out;
    }

    static void SendOptionsToPipe_IfAny()
    {
        if (!g_lastOptions.empty()) {
            PipeClient::Get().SendOptions(g_lastOptions);
        }
    }

    void LogOptionsIfChanged(const char* tag)
    {
        if (!DialogueVoiceFeaturesEnabled()) {
            return;
        }

        auto opts = SnapshotDialogueOptions();
        if (opts == g_lastOptions) {
            return;
        }

        g_lastOptions = opts;

        LogDebug(std::string("[OPTIONS][") + tag + "] count=" + std::to_string(opts.size()));
        for (size_t i = 0; i < opts.size(); ++i) {
            LogDebug("[OPTIONS][ITEM] " + std::to_string(i + 1) + ": " + opts[i]);
        }

        SendOptionsToPipe_IfAny();
    }

    static bool SelectDialogueIndex_Scaleform(int index0)
    {
        if (index0 < 0) {
            LogDebug("[SELECT][ERR] index < 0");
            return false;
        }

        auto ui = RE::UI::GetSingleton();
        if (!ui) {
            LogDebug("[SELECT][ERR] UI singleton nullptr");
            return false;
        }

        auto is = RE::InterfaceStrings::GetSingleton();
        if (!is) {
            LogDebug("[SELECT][ERR] InterfaceStrings nullptr");
            return false;
        }

        auto menu = ui->GetMenu(is->dialogueMenu);
        if (!menu) {
            LogDebug("[SELECT][ERR] GetMenu(dialogueMenu) nullptr");
            return false;
        }

        auto movie = menu->uiMovie.get();
        if (!movie) {
            LogDebug("[SELECT][ERR] uiMovie nullptr");
            return false;
        }

        RE::GFxValue argIndex;
        argIndex.SetNumber(static_cast<double>(index0));

        bool ok1 = movie->Invoke("_level0.DialogueMenu_mc.TopicList.SetSelectedTopic", nullptr, &argIndex, 1);
        bool ok2 = movie->Invoke("_level0.DialogueMenu_mc.TopicList.doSetSelectedIndex", nullptr, &argIndex, 1);
        bool ok3 = movie->Invoke("_level0.DialogueMenu_mc.TopicList.UpdateList", nullptr, nullptr, 0);

        RE::GFxValue argClick;
        argClick.SetNumber(1.0);
        bool ok4 = movie->Invoke("_level0.DialogueMenu_mc.onSelectionClick", nullptr, &argClick, 1);

        LogDebug("[SELECT] index0=" + std::to_string(index0) +
                " ok={SetSelectedTopic=" + std::string(ok1 ? "1" : "0") +
                " doSetSelectedIndex=" + std::string(ok2 ? "1" : "0") +
                " UpdateList=" + std::string(ok3 ? "1" : "0") +
                " onSelectionClick=" + std::string(ok4 ? "1" : "0") + "}");

        return ok1 && ok2 && ok4;
    }

    void RequestSelectIndex_MainThread(int index0, const std::string& text, float score)
    {
        bool expected = false;
        if (!g_selectInFlight.compare_exchange_strong(expected, true)) {
            LogDebug("[SELECT][SKIP] in flight");
            return;
        }

        LogDebug("[SELECT][REQ] index0=" + std::to_string(index0));

        const std::string option =
            (index0 >= 0 && static_cast<size_t>(index0) < g_lastOptions.size()) ? g_lastOptions[index0] : text;

        SKSE::GetTaskInterface()->AddTask([index0, text, option, score]() {
            std::this_thread::sleep_for(std::chrono::milliseconds(120));

            LogDebug("[SELECT][TASK] executing selection...");
            bool ok = SelectDialogueIndex_Scaleform(index0);
            const std::string status = ok ? "OK" : "FAIL";
            const std::string line =
                "Dialogue command recognized=" + Quote(text) +
                " result=select option=" + Quote(option) +
                " status=" + status +
                " score=" + FormatScore(score);
            if (ok) {
                LogInfo(line);
            } else {
                LogWarn(line);
            }

            g_selectInFlight.store(false);
        });
    }

    bool IsDialogueOpen()
    {
        return g_dialogueOpen.load();
    }

    class DialogueMenuWatcher : public RE::BSTEventSink<RE::MenuOpenCloseEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(
            const RE::MenuOpenCloseEvent* a_event,
            RE::BSTEventSource<RE::MenuOpenCloseEvent>*) override
        {
            if (!a_event) {
                return RE::BSEventNotifyControl::kContinue;
            }

            if (a_event->menuName == "Dialogue Menu"sv) {
                if (a_event->opening) {
                    g_dialogueOpen.store(true);
                    g_lastOptions.clear();

                    RefreshVoiceCommandState();

                    g_selectInFlight.store(false);

                    const bool dialogueVoiceFeatures = DialogueVoiceFeaturesEnabled();
                    g_dialogueVoiceSessionActive.store(dialogueVoiceFeatures);

                    if (dialogueVoiceFeatures) {
                        LogLine("[DIALOG] OPEN");
                        LogDebug("[SHOUTS] off (dialogue opened)");
                        LogOptionsIfChanged("OPEN");

                        auto mtm = RE::MenuTopicManager::GetSingleton();
                        LogDebug(std::string("[DBG] MTM=") + (mtm ? "OK" : "NULL") +
                                " dialogueList=" + ((mtm && mtm->dialogueList) ? "OK" : "NULL"));
                    }
                } else {
                    const bool hadDialogueVoiceSession = g_dialogueVoiceSessionActive.exchange(false);
                    if (hadDialogueVoiceSession) {
                        LogLine("[DIALOG] CLOSE");
                    }
                    g_dialogueOpen.store(false);
                    g_lastOptions.clear();

                    g_selectInFlight.store(false);
                    if (hadDialogueVoiceSession) {
                        PipeClient::Get().SendClose();
                    }
                    RefreshVoiceCommandState();
                }
            }

            return RE::BSEventNotifyControl::kContinue;
        }
    };

    static DialogueMenuWatcher g_dialogueWatcher;

    void RegisterDialogueWatcher()
    {
        if (auto ui = RE::UI::GetSingleton()) {
            ui->AddEventSink(&g_dialogueWatcher);
            LogDebug("[DIALOG] DialogueMenuWatcher registered");
        } else {
            LogLine("[DIALOG][WARN] UI singleton not available");
        }
    }
}
