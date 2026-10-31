; Inno Setup script for Logos AI Assistant (per-user install).
;
; Build:  & "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer\logos.iss
; Output: dist\LogosSetup.exe
;
; Per-user: the setup never elevates. The user picks the install folder
; (default %LOCALAPPDATA%\Programs\Logos), the Start Menu shortcut and the
; uninstall entry per user (HKCU "Installed apps" / Programs and Features),
; and the uninstaller removes the program plus its data folder
; %LOCALAPPDATA%\Logos (settings.json, logs, cache, prompts, assets) that the
; one-file build creates at first launch.
;
; Inno Setup is a build-time tool only; it is not a project dependency.

#define AppName "Logos AI Assistant"
#define AppVersion "0.1.0"
#define AppPublisher "AIgrator"
#define AppURL "https://github.com/AIgrator/Logos"
#define AppExeName "Logos.exe"

[Setup]
; A stable AppId ties upgrades and the uninstall entry to this application.
AppId={{8E2C4B7A-3F61-4D8E-9A5B-6C1D2E3F4A70}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
VersionInfoVersion=0.1.0.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Logos
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=LogosSetup
SetupIconFile=..\assets\ico.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
UsePreviousTasks=yes
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Logos"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Logos"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,Logos}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The user chose "delete everything" on uninstall: the one-file build keeps
; settings, logs, cache and materialized prompts/assets here, not in {app}.
Type: filesandordirs; Name: "{localappdata}\Logos"
