Scriptname DragonbornVoiceControlMCM extends SKI_ConfigBase

int _optDialogueSelect
int _optOpen
int _optClose
int _optShouts
int _optPowers
int _optMute
int _optWeapons
int _optSpells
int _optPotions
int _optQuickUsePotions
int _optUseBestPotion
int _optSpecifyHand
int _optQuickEquip
int _optKeyConsole
int _optPauseResume
int _optDebug
int _optDebugUnrecognized
int _optSaveWav
int _optRestart

Bool Function GetEnableVoiceOpen() Global Native
Bool Function GetEnableVoiceClose() Global Native
Bool Function GetEnableDialogueSelect() Global Native
Bool Function GetEnableVoiceShouts() Global Native
Bool Function GetMuteShoutVoiceLine() Global Native
Bool Function GetEnablePowers() Global Native
Bool Function GetEnableWeapons() Global Native
Bool Function GetEnableSpells() Global Native
Bool Function GetEnablePotions() Global Native
Bool Function GetQuickUsePotions() Global Native
Bool Function GetUseBestPotion() Global Native
Bool Function GetSpecifyHand() Global Native
Bool Function GetQuickEquip() Global Native
Bool Function GetEnableKeyConsole() Global Native
Bool Function GetEnablePauseResumePhrases() Global Native
Bool Function GetDebug() Global Native
Bool Function GetDebugUnrecognized() Global Native
Bool Function GetSaveWavCaptures() Global Native

Function SetEnableVoiceOpen(Bool value) Global Native
Function SetEnableVoiceClose(Bool value) Global Native
Function SetEnableDialogueSelect(Bool value) Global Native
Function SetEnableVoiceShouts(Bool value) Global Native
Function SetMuteShoutVoiceLine(Bool value) Global Native
Function SetEnablePowers(Bool value) Global Native
Function SetEnableWeapons(Bool value) Global Native
Function SetEnableSpells(Bool value) Global Native
Function SetEnablePotions(Bool value) Global Native
Function SetQuickUsePotions(Bool value) Global Native
Function SetUseBestPotion(Bool value) Global Native
Function SetSpecifyHand(Bool value) Global Native
Function SetQuickEquip(Bool value) Global Native
Function SetEnableKeyConsole(Bool value) Global Native
Function SetEnablePauseResumePhrases(Bool value) Global Native
Function SetDebug(Bool value) Global Native
Function SetDebugUnrecognized(Bool value) Global Native
Function SetSaveWavCaptures(Bool value) Global Native

Function RestartServer() Global Native

Event OnConfigInit()
    ModName = "Dragonborn Voice Control"
    Pages = new string[1]
    Pages[0] = "Settings"
EndEvent

Event OnPageReset(string page)
    SetCursorFillMode(TOP_TO_BOTTOM)

    SetCursorPosition(0)
    AddHeaderOption("Voice Features")

    _optDialogueSelect = AddToggleOption("Dialogue Select", GetEnableDialogueSelect())
    _optOpen  = AddToggleOption("Dialogue Open", GetEnableVoiceOpen())
    _optClose = AddToggleOption("Dialogue Close", GetEnableVoiceClose())
    _optShouts  = AddToggleOption("Shouts", GetEnableVoiceShouts())
    _optMute    = AddToggleOption("  Mute Shout Voice Line", GetMuteShoutVoiceLine())
    _optPowers = AddToggleOption("Powers", GetEnablePowers())
    _optSpells  = AddToggleOption("Spells", GetEnableSpells())
    _optWeapons = AddToggleOption("Weapons", GetEnableWeapons())
    _optQuickEquip = AddToggleOption("  Quick Equip", GetQuickEquip())
    _optSpecifyHand = AddToggleOption("  Specify Hand", GetSpecifyHand())
    _optPotions = AddToggleOption("Potions", GetEnablePotions())
    _optQuickUsePotions = AddToggleOption("  Quick Use Potions", GetQuickUsePotions())
    _optUseBestPotion = AddToggleOption("  Use Best Potion", GetUseBestPotion())
    _optKeyConsole = AddToggleOption("Key / Console commands", GetEnableKeyConsole())
    _optPauseResume = AddToggleOption("Pause / Resume commands", GetEnablePauseResumePhrases())

    SetCursorPosition(1)
    AddHeaderOption("System")

    _optDebug   = AddToggleOption("Debug Notifications", GetDebug())
    _optDebugUnrecognized = AddToggleOption("  Command Unrecognized Notifications", GetDebugUnrecognized())
    _optSaveWav = AddToggleOption("Save WAV Captures", GetSaveWavCaptures())
    _optRestart = AddTextOption("Restart Server", "Restart")
EndEvent

Event OnOptionSelect(int option)
    if option == _optDialogueSelect
        bool v = !GetEnableDialogueSelect()
        SetEnableDialogueSelect(v)
        SetToggleOptionValue(option, v)

    elseif option == _optOpen
        bool v = !GetEnableVoiceOpen()
        SetEnableVoiceOpen(v)
        SetToggleOptionValue(option, v)

    elseif option == _optClose
        bool v = !GetEnableVoiceClose()
        SetEnableVoiceClose(v)
        SetToggleOptionValue(option, v)

    elseif option == _optShouts
        bool v = !GetEnableVoiceShouts()
        SetEnableVoiceShouts(v)
        SetToggleOptionValue(option, v)

    elseif option == _optMute
        bool v = !GetMuteShoutVoiceLine()
        SetMuteShoutVoiceLine(v)
        SetToggleOptionValue(option, v)

    elseif option == _optPowers
        bool v = !GetEnablePowers()
        SetEnablePowers(v)
        SetToggleOptionValue(option, v)

    elseif option == _optWeapons
        bool v = !GetEnableWeapons()
        SetEnableWeapons(v)
        SetToggleOptionValue(option, v)

    elseif option == _optSpells
        bool v = !GetEnableSpells()
        SetEnableSpells(v)
        SetToggleOptionValue(option, v)

    elseif option == _optPotions
        bool v = !GetEnablePotions()
        SetEnablePotions(v)
        SetToggleOptionValue(option, v)

    elseif option == _optQuickUsePotions
        bool v = !GetQuickUsePotions()
        SetQuickUsePotions(v)
        SetToggleOptionValue(option, v)

    elseif option == _optUseBestPotion
        bool v = !GetUseBestPotion()
        SetUseBestPotion(v)
        SetToggleOptionValue(option, v)

    elseif option == _optSpecifyHand
        bool v = !GetSpecifyHand()
        SetSpecifyHand(v)
        SetToggleOptionValue(option, v)

    elseif option == _optQuickEquip
        bool v = !GetQuickEquip()
        SetQuickEquip(v)
        SetToggleOptionValue(option, v)

    elseif option == _optKeyConsole
        bool v = !GetEnableKeyConsole()
        SetEnableKeyConsole(v)
        SetToggleOptionValue(option, v)

    elseif option == _optPauseResume
        bool v = !GetEnablePauseResumePhrases()
        SetEnablePauseResumePhrases(v)
        SetToggleOptionValue(option, v)

    elseif option == _optDebug
        bool v = !GetDebug()
        SetDebug(v)
        SetToggleOptionValue(option, v)

    elseif option == _optDebugUnrecognized
        bool v = !GetDebugUnrecognized()
        SetDebugUnrecognized(v)
        SetToggleOptionValue(option, v)

    elseif option == _optSaveWav
        bool v = !GetSaveWavCaptures()
        SetSaveWavCaptures(v)
        SetToggleOptionValue(option, v)

    elseif option == _optRestart
        bool ok = ShowMessage("Restart the voice server now?", true, "Restart", "Cancel")
        if ok
            RestartServer()
        endif
    endif
EndEvent

Event OnOptionHighlight(int option)
    if option == _optDialogueSelect
        SetInfoText("Enable voice selection of dialogue options.")

    elseif option == _optOpen
        SetInfoText("Enable voice open commands to open dialogue when looking at an NPC. Open commands must be configured in DVCRuntime.ini.")

    elseif option == _optClose
        SetInfoText("Enable voice close commands to close dialogue. Close commands must be configured in DVCRuntime.ini.")

    elseif option == _optShouts
        SetInfoText("Use favorite shouts by speaking shout words.")

    elseif option == _optMute
        SetInfoText("Suppress the Dovahkiin voice line when a shout is triggered via voice.")

    elseif option == _optPowers
        SetInfoText("Use favorite powers by speaking their name.")

    elseif option == _optWeapons
        SetInfoText("Equip favorite weapons by speaking their name.")

    elseif option == _optSpells
        SetInfoText("Equip favorite spells by speaking their name.")

    elseif option == _optPotions
        SetInfoText("Use favorite potions by speaking their name.")

    elseif option == _optQuickUsePotions
        SetInfoText("Enable quick-use potion commands - 'Health potion / Healing potion', 'Magicka potion / Mana potion', or 'Stamina potion'. Group names can be changed in DVCRuntime.ini.")

    elseif option == _optUseBestPotion
        SetInfoText("Use the strongest matching favorited potion instead of the weakest one.")

    elseif option == _optSpecifyHand
        SetInfoText("Allow saying hand suffixes 'Left', 'Right', or 'Both' after weapon and spell names. Suffix words can be changed in DVCRuntime.ini")

    elseif option == _optQuickEquip
        SetInfoText("Enable quick equip weapons by equipment type such as 'Sword', 'Bow', 'Shield', etc. Types can be changed in DVCRuntime.ini.")

    elseif option == _optKeyConsole
        SetInfoText("Enable voice commands to activate console commands and key presses. Console and key commands must be configured in DVCRuntime.ini.")

    elseif option == _optPauseResume
        SetInfoText("Enable voice commands 'Stop speech recognition' and 'Start speech recognition' that disable and enable other voice commands. Pause and resume commands can be changed in DVCRuntime.ini.")

    elseif option == _optDebug
        SetInfoText("Show in-game recognition notifications.")

    elseif option == _optDebugUnrecognized
        SetInfoText("Show unrecognized command notifications. If Debug Notifications is on, these messages are always shown.")

    elseif option == _optSaveWav
        SetInfoText("Save captured voice audio to DVCRuntime/caches/vad_caps/*.wav for debugging.")

    elseif option == _optRestart
        SetInfoText("Restart the voice recognition local server.")
    endif
EndEvent