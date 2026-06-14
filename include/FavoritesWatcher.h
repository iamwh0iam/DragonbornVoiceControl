#pragma once

namespace DragonbornVoiceControl
{
    /// Register menu-close event sinks to detect favorites / magic menu changes.
    /// On FavoritesMenu close → rescan favorites for weapons, spells, potions, powers, shouts.
    /// On MagicMenu close   → rescan shouts (player may have spent dragon souls).
    void RegisterFavoritesWatcher();

    bool AnyFavoritesFeatureEnabled();

    /// Full scan of all enabled categories.  Called on save load and reconnect.
    void ScanAllFavorites(bool force);
}
