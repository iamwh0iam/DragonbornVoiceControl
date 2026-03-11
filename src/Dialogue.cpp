#include "PCH.h"

#include "Dialogue.h"

#include "Logging.h"
#include "PipeClient.h"
#include "Runtime.h"
#include "Settings.h"

#include <atomic>
#include <chrono>
#include <string>
#include <thread>
#include <vector>

namespace DragonbornVoiceControl
{
    std::vector<std::string> g_lastOptions;
    static std::atomic_bool g_selectInFlight{ false };
    static std::atomic_bool g_dialogueOpen{ false };

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
        auto opts = SnapshotDialogueOptions();
        if (opts == g_lastOptions) {
            return;
        }

        g_lastOptions = opts;

        LogLine(std::string("[OPTIONS][") + tag + "] count=" + std::to_string(opts.size()));
        for (size_t i = 0; i < opts.size(); ++i) {
            LogLine("[OPTIONS][ITEM] " + std::to_string(i + 1) + ": " + opts[i]);
        }

        SendOptionsToPipe_IfAny();
    }

    static bool SelectDialogueIndex_Scaleform(int index0)
    {
        if (index0 < 0) {
            LogLine("[SELECT][ERR] index < 0");
            return false;
        }

        auto ui = RE::UI::GetSingleton();
        if (!ui) {
            LogLine("[SELECT][ERR] UI singleton nullptr");
            return false;
        }

        auto is = RE::InterfaceStrings::GetSingleton();
        if (!is) {
            LogLine("[SELECT][ERR] InterfaceStrings nullptr");
            return false;
        }

        auto menu = ui->GetMenu(is->dialogueMenu);
        if (!menu) {
            LogLine("[SELECT][ERR] GetMenu(dialogueMenu) nullptr");
            return false;
        }

        auto movie = menu->uiMovie.get();
        if (!movie) {
            LogLine("[SELECT][ERR] uiMovie nullptr");
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

        LogLine("[SELECT] index0=" + std::to_string(index0) +
                " ok={SetSelectedTopic=" + std::string(ok1 ? "1" : "0") +
                " doSetSelectedIndex=" + std::string(ok2 ? "1" : "0") +
                " UpdateList=" + std::string(ok3 ? "1" : "0") +
                " onSelectionClick=" + std::string(ok4 ? "1" : "0") + "}");

        return ok1 && ok2 && ok4;
    }

    void RequestSelectIndex_MainThread(int index0)
    {
        bool expected = false;
        if (!g_selectInFlight.compare_exchange_strong(expected, true)) {
            LogLine("[SELECT][SKIP] in flight");
            return;
        }

        LogLine("[SELECT][REQ] index0=" + std::to_string(index0));

        SKSE::GetTaskInterface()->AddTask([index0]() {
            std::this_thread::sleep_for(std::chrono::milliseconds(120));

            LogLine("[SELECT][TASK] executing selection...");
            bool ok = SelectDialogueIndex_Scaleform(index0);
            LogLine(std::string("[SELECT][TASK] result=") + (ok ? "OK" : "FAIL"));

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
                    LogLine("[DIALOG] OPEN");
                    g_dialogueOpen.store(true);
                    g_lastOptions.clear();

                    RefreshVoiceCommandState();
                    LogLine("[SHOUTS] off (dialogue opened)");

                    g_selectInFlight.store(false);

                    LogOptionsIfChanged("OPEN");

                    auto mtm = RE::MenuTopicManager::GetSingleton();
                    LogLine(std::string("[DBG] MTM=") + (mtm ? "OK" : "NULL") +
                            " dialogueList=" + ((mtm && mtm->dialogueList) ? "OK" : "NULL"));
                } else {
                    LogLine("[DIALOG] CLOSE");
                    g_dialogueOpen.store(false);
                    g_lastOptions.clear();

                    g_selectInFlight.store(false);
                    PipeClient::Get().SendClose();
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
            LogLine("[DIALOG] DialogueMenuWatcher registered");
        } else {
            LogLine("[DIALOG][WARN] UI singleton not available");
        }
    }
}
