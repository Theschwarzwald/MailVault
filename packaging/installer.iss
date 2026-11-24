; Inno Setup script - builds MailVault-Setup.exe with file associations
; Build: pyinstaller --noconfirm packaging/mailvault.spec, then iscc packaging\installer.iss
#define AppVersion "2.9.0"

[Setup]
AppId={{6B1F3C2E-4D8A-4E5B-9C7D-2A1E0F3B5C84}
AppName=MailVault
AppVersion={#AppVersion}
AppVerName=MailVault {#AppVersion}
AppPublisher=MailVault
DefaultDirName={autopf}\MailVault
DefaultGroupName=MailVault
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=MailVault-Setup-{#AppVersion}
SetupIconFile=mailvault.ico
UninstallDisplayIcon={app}\MailVault.exe
ChangesAssociations=yes
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\dist\MailVault\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\MailVault"; Filename: "{app}\MailVault.exe"
Name: "{autodesktop}\MailVault"; Filename: "{app}\MailVault.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"
Name: assoc; Description: "Open .pst, .ost, .msg, .eml, .emlx and .mbox files with MailVault"

[Registry]
Root: HKA; Subkey: "Software\Classes\MailVault.File"; ValueType: string; ValueData: "Mail file"; Flags: uninsdeletekey; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\MailVault.File\DefaultIcon"; ValueType: string; ValueData: "{app}\MailVault.exe,0"; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\MailVault.File\shell\open\command"; ValueType: string; ValueData: """{app}\MailVault.exe"" ""%1"""; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\.pst\OpenWithProgids"; ValueType: string; ValueName: "MailVault.File"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\.ost\OpenWithProgids"; ValueType: string; ValueName: "MailVault.File"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\.msg\OpenWithProgids"; ValueType: string; ValueName: "MailVault.File"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\.eml\OpenWithProgids"; ValueType: string; ValueName: "MailVault.File"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\.emlx\OpenWithProgids"; ValueType: string; ValueName: "MailVault.File"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\.mbox\OpenWithProgids"; ValueType: string; ValueName: "MailVault.File"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\.mbx\OpenWithProgids"; ValueType: string; ValueName: "MailVault.File"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assoc

[Run]
Filename: "{app}\MailVault.exe"; Description: "Open MailVault"; Flags: nowait postinstall skipifsilent
