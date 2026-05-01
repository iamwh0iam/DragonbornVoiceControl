#include "PCH.h"
#include "FavoritesWatcher.h"

#include "Common.h"
#include "Logging.h"
#include "PipeClient.h"
#include "Settings.h"
#include "VoiceHandle.h"
#include "ShoutsInternal.h"

#include <algorithm>
#include <string>
#include <unordered_set>
#include <vector>

namespace DragonbornVoiceControl
{
    // ── helpers ─────────────────────────────────────────────

    static std::string SafeName(const char* raw)
    {
        return detail::SafeGameString(raw);
    }

    static bool EntriesEqual(const std::vector<ShoutEntry>& a, const std::vector<ShoutEntry>& b)
    {
        if (a.size() != b.size()) return false;
        return std::equal(a.begin(), a.end(), b.begin(), [](const ShoutEntry& lhs, const ShoutEntry& rhs) {
            return lhs.plugin == rhs.plugin && lhs.formIdHex == rhs.formIdHex && lhs.name == rhs.name && lhs.editorID == rhs.editorID;
        });
    }

    static bool EntriesEqual(const std::vector<PowerEntry>& a, const std::vector<PowerEntry>& b)
    {
        if (a.size() != b.size()) return false;
        return std::equal(a.begin(), a.end(), b.begin(), [](const PowerEntry& lhs, const PowerEntry& rhs) {
            return lhs.formIdHex == rhs.formIdHex && lhs.name == rhs.name;
        });
    }

    static bool EntriesEqual(const std::vector<ItemEntry>& a, const std::vector<ItemEntry>& b)
    {
        if (a.size() != b.size()) return false;
        return std::equal(a.begin(), a.end(), b.begin(), [](const ItemEntry& lhs, const ItemEntry& rhs) {
            return lhs.formIdHex == rhs.formIdHex && lhs.name == rhs.name;
        });
    }

    static std::string FormatItemList(const std::vector<ItemEntry>& entries)
    {
        std::string out;
        for (size_t i = 0; i < entries.size(); ++i) {
            if (i > 0) out += ", ";
            out += entries[i].formIdHex;
            out += ' ';
            out += entries[i].name;
        }
        return out;
    }

    static std::string FormatPowerList(const std::vector<PowerEntry>& entries)
    {
        std::string out;
        for (size_t i = 0; i < entries.size(); ++i) {
            if (i > 0) out += ", ";
            out += entries[i].formIdHex;
            out += ' ';
            out += entries[i].name;
        }
        return out;
    }

    static std::string FormatShoutList(const std::vector<ShoutEntry>& entries)
    {
        std::string out;
        for (size_t i = 0; i < entries.size(); ++i) {
            if (i > 0) out += ", ";
            out += entries[i].formIdHex;
            out += ' ';
            out += entries[i].name;
        }
        return out;
    }

    static std::vector<ShoutEntry> g_lastShouts;
    static std::vector<PowerEntry> g_lastPowers;
    static std::vector<ItemEntry> g_lastWeapons;
    static std::vector<ItemEntry> g_lastSpells;
    static std::vector<ItemEntry> g_lastPotions;

    // ── Favorite scanning ───────────────────────────────────

    static std::vector<ItemEntry> CollectFavoriteWeapons()
    {
        if (!IsWeaponsEnabled()) return {};

        auto* fav = RE::MagicFavorites::GetSingleton();
        if (!fav) return {};

        // Weapons are in InventoryChanges, not MagicFavorites.
        // MagicFavorites only tracks spells/shouts/powers.
        // For weapons we need to check the player's inventory and hotkeys.
        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) return {};

        // Gather favorited weapon form IDs from hotkeys
        std::unordered_set<RE::FormID> favWeaponIds;

        // Check hotkeys (weapons can appear in hotkeys array)
        for (auto* f : fav->hotkeys) {
            if (!f) continue;
            if (auto* weap = f->As<RE::TESObjectWEAP>()) {
                favWeaponIds.insert(weap->GetFormID());
            }
        }

        // Also check InventoryChanges for items flagged as favorite
        auto* invChanges = player->GetInventoryChanges();
        if (invChanges && invChanges->entryList) {
            for (auto* entry : *invChanges->entryList) {
                if (!entry || !entry->object) continue;
                auto* weap = entry->object->As<RE::TESObjectWEAP>();
                if (!weap) continue;

                // Check if this item is in favorites via extra data
                if (entry->extraLists) {
                    for (auto* extraList : *entry->extraLists) {
                        if (extraList && extraList->HasType(RE::ExtraDataType::kHotkey)) {
                            favWeaponIds.insert(weap->GetFormID());
                        }
                    }
                }
            }
        }

        std::vector<ItemEntry> entries;
        for (RE::FormID id : favWeaponIds) {
            auto* form = RE::TESForm::LookupByID<RE::TESObjectWEAP>(id);
            if (!form) continue;
            std::string name = SafeName(form->GetFullName());
            if (name.empty()) continue;
            ItemEntry e;
            e.formIdHex = std::string("0x") + detail::FormIDToHex(id);
            e.name = std::move(name);
            entries.push_back(std::move(e));
        }

        std::sort(entries.begin(), entries.end(),
            [](const ItemEntry& a, const ItemEntry& b) { return a.formIdHex < b.formIdHex; });

        return entries;
    }

    static std::vector<ItemEntry> CollectFavoriteSpells()
    {
        if (!IsSpellsEnabled()) return {};

        auto* fav = RE::MagicFavorites::GetSingleton();
        if (!fav) return {};

        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) return {};

        std::vector<ItemEntry> entries;

        auto scanArray = [&](const RE::BSTArray<RE::TESForm*>& arr) {
            for (auto* f : arr) {
                if (!f) continue;
                auto* spell = f->As<RE::SpellItem>();
                if (!spell) continue;

                // Exclude powers and lesser powers (those are handled by the powers system)
                const auto type = spell->GetSpellType();
                if (type == RE::MagicSystem::SpellType::kPower ||
                    type == RE::MagicSystem::SpellType::kLesserPower) {
                    continue;
                }

                if (!player->HasSpell(spell)) continue;

                std::string name = SafeName(spell->GetFullName());
                if (name.empty()) continue;

                ItemEntry e;
                e.formIdHex = std::string("0x") + detail::FormIDToHex(spell->GetFormID());
                e.name = std::move(name);
                entries.push_back(std::move(e));
            }
        };
        scanArray(fav->spells);
        scanArray(fav->hotkeys);

        // Deduplicate
        std::sort(entries.begin(), entries.end(),
            [](const ItemEntry& a, const ItemEntry& b) { return a.formIdHex < b.formIdHex; });
        entries.erase(std::unique(entries.begin(), entries.end(),
            [](const ItemEntry& a, const ItemEntry& b) { return a.formIdHex == b.formIdHex; }),
            entries.end());

        return entries;
    }

    static std::vector<ItemEntry> CollectFavoritePotions()
    {
        if (!IsPotionsEnabled()) return {};

        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) return {};

        auto* fav = RE::MagicFavorites::GetSingleton();
        if (!fav) return {};

        // Potions are inventory items. Check hotkeys + inventory favorites.
        std::unordered_set<RE::FormID> favPotionIds;

        for (auto* f : fav->hotkeys) {
            if (!f) continue;
            if (auto* alch = f->As<RE::AlchemyItem>()) {
                favPotionIds.insert(alch->GetFormID());
            }
        }

        auto* invChanges = player->GetInventoryChanges();
        if (invChanges && invChanges->entryList) {
            for (auto* entry : *invChanges->entryList) {
                if (!entry || !entry->object) continue;
                auto* alch = entry->object->As<RE::AlchemyItem>();
                if (!alch) continue;

                if (entry->extraLists) {
                    for (auto* extraList : *entry->extraLists) {
                        if (extraList && extraList->HasType(RE::ExtraDataType::kHotkey)) {
                            favPotionIds.insert(alch->GetFormID());
                        }
                    }
                }
            }
        }

        std::vector<ItemEntry> entries;
        for (RE::FormID id : favPotionIds) {
            auto* form = RE::TESForm::LookupByID<RE::AlchemyItem>(id);
            if (!form) continue;

            // Skip poisons (only consumable potions)
            if (form->IsPoison()) continue;

            std::string name = SafeName(form->GetFullName());
            if (name.empty()) continue;

            // Check player actually has this item
            auto inv = player->GetInventory();
            bool hasItem = false;
            for (auto& [obj, data] : inv) {
                if (obj && obj->GetFormID() == id && data.first > 0) {
                    hasItem = true;
                    break;
                }
            }
            if (!hasItem) continue;

            ItemEntry e;
            e.formIdHex = std::string("0x") + detail::FormIDToHex(id);
            e.name = std::move(name);
            entries.push_back(std::move(e));
        }

        std::sort(entries.begin(), entries.end(),
            [](const ItemEntry& a, const ItemEntry& b) { return a.formIdHex < b.formIdHex; });

        return entries;
    }

    // ── Shout scanning ────────────────────────────────────────

    static std::vector<ShoutEntry> CollectShouts()
    {
        if (!IsVoiceShoutsEnabled()) return {};

        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) return {};

        auto* dataHandler = RE::TESDataHandler::GetSingleton();
        if (!dataHandler) return {};

        // Gather favorited shout IDs
        std::unordered_set<RE::FormID> favSet;
        if (auto* fav = RE::MagicFavorites::GetSingleton(); fav) {
            for (auto* f : fav->spells)  { if (f) if (auto* s = f->As<RE::TESShout>()) favSet.insert(s->GetFormID()); }
            for (auto* f : fav->hotkeys) { if (f) if (auto* s = f->As<RE::TESShout>()) favSet.insert(s->GetFormID()); }
        }

        std::vector<ShoutEntry> allowed;

        auto& shouts = dataHandler->GetFormArray<RE::TESShout>();
        for (auto* shout : shouts) {
            if (!shout) continue;

            // Must have at least one valid word/spell variation
            bool valid = false;
            for (int vi = 0; vi < 3; ++vi) {
                const auto& var = shout->variations[vi];
                if (var.word || var.spell) { valid = true; break; }
            }
            if (!valid) continue;
            if (!shout->GetKnown()) continue;

            const RE::FormID id = shout->GetFormID();
            const bool isFav = favSet.count(id) > 0;

            if (!isFav) continue;

            std::string name = SafeName(shout->GetFullName());
            std::string editorID = SafeName(shout->GetFormEditorID());
            std::string pluginName;
            if (const auto* file = shout->GetFile(0)) {
                if (file->fileName) {
                    pluginName = SafeName(file->fileName);
                }
            }
            if (pluginName.empty()) {
                continue;
            }

            ShoutEntry entry;
            entry.plugin = std::move(pluginName);
            entry.formIdHex = std::string("0x") + detail::FormIDToHex(id & 0x00FFFFFF);
            entry.name = std::move(name);
            entry.editorID = std::move(editorID);
            allowed.push_back(std::move(entry));

        }

        std::sort(allowed.begin(), allowed.end(),
            [](const ShoutEntry& a, const ShoutEntry& b) {
                if (a.plugin != b.plugin) return a.plugin < b.plugin;
                return a.formIdHex < b.formIdHex;
            });

        return allowed;
    }

    // ── Power scanning ──────────────────────────────────────

    static std::vector<PowerEntry> CollectPowers()
    {
        if (!IsVoiceShoutsEnabled() || !IsEnablePowersEnabled()) return {};

        auto* player = RE::PlayerCharacter::GetSingleton();
        if (!player) return {};

        auto* dataHandler = RE::TESDataHandler::GetSingleton();
        if (!dataHandler) return {};

        // Gather favorited spell/power IDs
        std::unordered_set<RE::FormID> favSet;
        if (auto* fav = RE::MagicFavorites::GetSingleton(); fav) {
            for (auto* f : fav->spells)  { if (f) if (auto* s = f->As<RE::SpellItem>()) favSet.insert(s->GetFormID()); }
            for (auto* f : fav->hotkeys) { if (f) if (auto* s = f->As<RE::SpellItem>()) favSet.insert(s->GetFormID()); }
        }

        std::vector<PowerEntry> allowed;

        auto& spells = dataHandler->GetFormArray<RE::SpellItem>();
        for (auto* spell : spells) {
            if (!spell) continue;

            const auto type = spell->GetSpellType();
            if (type != RE::MagicSystem::SpellType::kPower &&
                type != RE::MagicSystem::SpellType::kLesserPower) {
                continue;
            }
            if (!player->HasSpell(spell)) continue;

            const RE::FormID id = spell->GetFormID();
            const bool isFav = favSet.count(id) > 0;

            if (!isFav) continue;

            std::string name = SafeName(spell->GetFullName());
            if (name.empty()) continue;

            PowerEntry entry;
            entry.formIdHex = std::string("0x") + detail::FormIDToHex(id);
            entry.name = std::move(name);
            allowed.push_back(std::move(entry));

        }

        std::sort(allowed.begin(), allowed.end(),
            [](const PowerEntry& a, const PowerEntry& b) { return a.formIdHex < b.formIdHex; });

        return allowed;
    }

    // ── Public scan entry point ─────────────────────────────

    void ScanAllFavorites(bool force)
    {
        const bool debug = IsDebugEnabled();

        auto shouts = CollectShouts();
        auto powers = CollectPowers();
        auto weapons = CollectFavoriteWeapons();
        auto spells = CollectFavoriteSpells();
        auto potions = CollectFavoritePotions();

        const bool changed =
            !EntriesEqual(shouts, g_lastShouts) ||
            !EntriesEqual(powers, g_lastPowers) ||
            !EntriesEqual(weapons, g_lastWeapons) ||
            !EntriesEqual(spells, g_lastSpells) ||
            !EntriesEqual(potions, g_lastPotions);

        if (changed || force) {
            PipeClient::Get().SendAllFavorites(shouts, powers, weapons, spells, potions);
        }

        if (changed) {
            LogLine("[FAV] Update detected");
            LogLine("[FAV] shouts=" + std::to_string(shouts.size()) +
                    " powers=" + std::to_string(powers.size()) +
                    " weapons=" + std::to_string(weapons.size()) +
                    " spells=" + std::to_string(spells.size()) +
                    " potions=" + std::to_string(potions.size()));

            LogLine("[FAV] Shouts: [" + FormatShoutList(shouts) + "] "
                "Powers: [" + FormatPowerList(powers) + "] "
                "Weapons: [" + FormatItemList(weapons) + "] "
                "Spells: [" + FormatItemList(spells) + "] "
                "Potions: [" + FormatItemList(potions) + "]");

            if (debug) {
                if (!shouts.empty())  LogLine("[FAV][SHOUTS] (" + FormatShoutList(shouts) + ")");
                if (!powers.empty())  LogLine("[FAV][POWERS] (" + FormatPowerList(powers) + ")");
                if (!weapons.empty()) LogLine("[FAV][WEAPONS] (" + FormatItemList(weapons) + ")");
                if (!spells.empty())  LogLine("[FAV][SPELLS] (" + FormatItemList(spells) + ")");
                if (!potions.empty()) LogLine("[FAV][POTIONS] (" + FormatItemList(potions) + ")");
            }
        } else if (debug) {
            LogLine("[FAV] ScanAllFavorites force=" + std::to_string(force) + " (no changes)");
        }

        g_lastShouts = std::move(shouts);
        g_lastPowers = std::move(powers);
        g_lastWeapons = std::move(weapons);
        g_lastSpells = std::move(spells);
        g_lastPotions = std::move(potions);
    }

    // ── Menu watcher ────────────────────────────────────────

    class FavoritesMenuWatcher : public RE::BSTEventSink<RE::MenuOpenCloseEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(
            const RE::MenuOpenCloseEvent* a_event,
            RE::BSTEventSource<RE::MenuOpenCloseEvent>*) override
        {
            if (!a_event || a_event->opening) {
                return RE::BSEventNotifyControl::kContinue;
            }

            // FavoritesMenu, MagicMenu, or InventoryMenu closed → full rescan of all categories
            if (a_event->menuName == "FavoritesMenu"sv ||
                a_event->menuName == "MagicMenu"sv ||
                a_event->menuName == "InventoryMenu"sv) {
                LogLine(std::string("[FAV] ") + a_event->menuName.data() + " closed, scheduling rescan");
                if (auto* t = SKSE::GetTaskInterface(); t) {
                    t->AddTask([]() {
                        ScanAllFavorites(false);
                    });
                }
            }

            return RE::BSEventNotifyControl::kContinue;
        }
    };

    static FavoritesMenuWatcher g_favoritesWatcher;

    void RegisterFavoritesWatcher()
    {
        if (auto ui = RE::UI::GetSingleton()) {
            ui->AddEventSink(&g_favoritesWatcher);
            LogLine("[FAV] FavoritesMenuWatcher registered");
        } else {
            LogLine("[FAV][WARN] UI singleton not available for favorites watcher");
        }
    }

} // namespace DragonbornVoiceControl
