; Mia for Windows. Filled in by build.ps1 (@VERSION@, @EXT_ID@, @STAGE@, @OUT@).
; Per-user install, no administrator prompt. Chrome finds the helper through
; HKCU registry keys and offers the store extension by itself.
[Setup]
AppId={{7B6E7B51-9E5B-4C6F-9C2A-5A1D4B0C01A7}
AppName=Mia
AppVersion=@VERSION@
AppPublisher=Mia Labs
AppPublisherURL=https://github.com/luislozanogmia/mia-browser-use
DefaultDirName={localappdata}\Mia
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=@OUT@
OutputBaseFilename=Mia-Browser-Use-Setup-@VERSION@
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayName=Mia (browser assistant helper)
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Messages]
english.WelcomeLabel2=This installs the Mia helper on this computer.%n%nMia is your browser assistant in Chrome. The helper is the part that runs on your PC (Mia doesn't run in the cloud): Mia's programs with their own Python, and the small program Chrome starts when the Mia extension asks for it.%n%nMia answers with your own Claude account (Anthropic). If Claude Code is missing, Mia's panel offers to install it with Anthropic's official installer.%n%nIf Chrome doesn't have the Mia extension yet, Chrome offers it the next time it starts.
english.FinishedLabelNoIcons=If Mia is already in Chrome, her panel connects by itself in a few seconds.%n%nIf not, open Chrome (or close and reopen it). Chrome says a new extension, Mia, was added and asks to turn it on: click Enable. Then click the Mia icon in Chrome's toolbar and Sign in to Claude the first time.

[Files]
Source: "@STAGE@\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
; Where the helper is, for each Chrome-family browser (current user).
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.ghost.bridge"; ValueType: string; ValueData: "{app}\com.ghost.bridge.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Chromium\NativeMessagingHosts\com.ghost.bridge"; ValueType: string; ValueData: "{app}\com.ghost.bridge.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\com.ghost.bridge"; ValueType: string; ValueData: "{app}\com.ghost.bridge.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.ghost.bridge"; ValueType: string; ValueData: "{app}\com.ghost.bridge.json"; Flags: uninsdeletekey
; Chrome offers the store extension by itself on its next start.
Root: HKCU; Subkey: "Software\Google\Chrome\Extensions\@EXT_ID@"; ValueType: string; ValueName: "update_url"; ValueData: "https://clients2.google.com/service/update2/crx"; Flags: uninsdeletekey

[Run]
; An older helper would keep running old code; Chrome starts the new one.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\stop-helper.ps1"""; Flags: runhidden; StatusMsg: "Stopping the old helper"

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\stop-helper.ps1"""; Flags: runhidden; RunOnceId: "StopMia"
