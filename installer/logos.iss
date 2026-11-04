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
; one-file build creates at first launch. The Chromium engine in
; %LOCALAPPDATA%\ms-playwright (~700 MB) is outside both folders and may be
; shared with other Playwright programs, so the uninstaller asks about it:
; a checkbox that is off by default.
;
; The optional tasks mirror what the first-run wizard asks in a source
; checkout: the global 'logos' command (adds {app} to the user PATH), the
; "Open with Logos" folder context menu and the desktop shortcut. An
; installed build gets no second wizard page (src/initial_setup.py), so these
; integrations happen here and the uninstaller reverses them.
;
; Inno Setup is a build-time tool only; it is not a project dependency.

#define AppName "Logos AI Assistant"
; The release number is not repeated here: it is read from the built EXE, whose
; version resource logos.spec generates from [project] version in pyproject.toml.
; Build dist\Logos.exe first (pyinstaller logos.spec), then compile this script.
#define AppExePath AddBackslash(SourcePath) + "..\dist\Logos.exe"
#define AppVersion GetStringFileInfo(AppExePath, "ProductVersion")
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
; The file version string is the same three-part number: GetFileVersion() would
; pad it to four (0.3.1.0) and that trailing zero is what Explorer shows.
VersionInfoVersion={#AppVersion}
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
; Our PATH edit is done in [Code]; let Setup broadcast WM_SETTINGCHANGE so
; Explorer (and terminals opened from it) see the new 'logos' command.
ChangesEnvironment=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
english.WindowsIntegration=Windows integration:
russian.WindowsIntegration=Интеграция в Windows:
english.CreateGlobalCommand=Create global 'logos' command (recommended)
russian.CreateGlobalCommand=Создать глобальную команду 'logos' (рекомендуется)
english.AddContextMenu=Add 'Open with Logos' to the folder right-click menu
russian.AddContextMenu=Добавить «Открыть с помощью Logos» в контекстное меню папок
english.UninstallBrowserEngineTitle=Uninstall {#AppName}
russian.UninstallBrowserEngineTitle=Удаление {#AppName}
english.UninstallBrowserEngineText=Browser mode uses Chromium, which lives outside the program folder in %LOCALAPPDATA%\ms-playwright and takes about 700 MB. Other programs on this computer may use the same copy, so it is kept by default. The program itself is removed together with its settings, logs and browser profiles.
russian.UninstallBrowserEngineText=Режим Browser использует Chromium, который лежит вне папки программы, в %LOCALAPPDATA%\ms-playwright, и занимает около 700 МБ. Эту копию могут использовать и другие программы на компьютере, поэтому по умолчанию она сохраняется. Сама программа удаляется вместе с настройками, журналами и профилями браузера.
english.UninstallBrowserEngineCheck=Also delete the browser engine (Chromium, ~700 MB)
russian.UninstallBrowserEngineCheck=Также удалить движок браузера (Chromium, ~700 МБ)
english.UninstallBrowserEngineOk=Uninstall
russian.UninstallBrowserEngineOk=Удалить
english.UninstallBrowserEngineCancel=Cancel
russian.UninstallBrowserEngineCancel=Отмена

[Tasks]
Name: "addtopath"; Description: "{cm:CreateGlobalCommand}"; GroupDescription: "{cm:WindowsIntegration}"
Name: "contextmenu"; Description: "{cm:AddContextMenu}"; GroupDescription: "{cm:WindowsIntegration}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "logos.cmd"; DestDir: "{app}"; Flags: ignoreversion; Tasks: addtopath

[Registry]
Root: HKCU; Subkey: "Software\Classes\Directory\shell\Logos"; ValueType: string; ValueName: ""; ValueData: "Open with Logos"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Directory\shell\Logos"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#AppExeName}"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Directory\shell\Logos\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

[Icons]
Name: "{autoprograms}\Logos"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Logos"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,Logos}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The user chose "delete everything" on uninstall: the one-file build keeps
; settings, logs, cache and materialized prompts/assets here, not in {app}.
Type: filesandordirs; Name: "{localappdata}\Logos"

[Code]
const
  UserEnvKey = 'Environment';

{ Chromium for Browser mode is downloaded at first use into the Playwright
  browser folder, outside the program folder and outside the app data folder.
  It is shared with any other Playwright program on the machine, so the
  uninstaller only removes it when the user ticks the box: the box starts
  empty and the whole question is skipped when the folder is not there. }
var
  DeleteBrowserEngine: Boolean;

function BrowserEngineDir(): string;
begin
  Result := ExpandConstant('{localappdata}\ms-playwright');
end;

function AskDeleteBrowserEngine(): Boolean;
var
  Form: TSetupForm;
  Info: TNewStaticText;
  Choice: TNewCheckBox;
  OkButton, CancelButton: TNewButton;
  ButtonWidth: Integer;
begin
  Result := False;
  Form := CreateCustomForm(460, 230, False, True);
  try
    Form.Caption := ExpandConstant('{cm:UninstallBrowserEngineTitle}');

    Info := TNewStaticText.Create(Form);
    Info.Parent := Form;
    Info.Left := ScaleX(12);
    Info.Top := ScaleY(12);
    Info.Width := Form.ClientWidth - ScaleX(24);
    Info.Height := ScaleY(84);
    Info.AutoSize := False;
    Info.WordWrap := True;
    Info.Caption := ExpandConstant('{cm:UninstallBrowserEngineText}');

    Choice := TNewCheckBox.Create(Form);
    Choice.Parent := Form;
    Choice.Left := ScaleX(12);
    Choice.Top := ScaleY(104);
    Choice.Width := Form.ClientWidth - ScaleX(24);
    Choice.Caption := ExpandConstant('{cm:UninstallBrowserEngineCheck}');
    Choice.Checked := False;

    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Caption := ExpandConstant('{cm:UninstallBrowserEngineOk}');
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;
    OkButton.Height := ScaleY(23);
    OkButton.Top := Form.ClientHeight - ScaleY(33);

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := ExpandConstant('{cm:UninstallBrowserEngineCancel}');
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    CancelButton.Height := ScaleY(23);
    CancelButton.Top := OkButton.Top;

    ButtonWidth := Form.CalculateButtonWidth([OkButton.Caption, CancelButton.Caption]);
    OkButton.Width := ButtonWidth;
    CancelButton.Width := ButtonWidth;
    OkButton.Left := Form.ClientWidth - ScaleX(12) - CancelButton.Width - ScaleX(8) - OkButton.Width;
    CancelButton.Left := Form.ClientWidth - ScaleX(12) - CancelButton.Width;

    if Form.ShowModal() = mrOk then
    begin
      Result := True;
      DeleteBrowserEngine := Choice.Checked;
    end;
  finally
    Form.Free();
  end;
end;

function InitializeUninstall(): Boolean;
begin
  DeleteBrowserEngine := False;
  { Silent uninstall (scripts, package managers) never prompts: keep the engine. }
  if UninstallSilent() then
  begin
    Result := True;
    Exit;
  end;
  if not DirExists(BrowserEngineDir()) then
  begin
    Result := True;
    Exit;
  end;
  Result := AskDeleteBrowserEngine();
end;

function NormalizePathEntry(const Value: string): string;
var
  S: string;
begin
  S := Trim(Value);
  if (Length(S) >= 2) and (Copy(S, 1, 1) = '"') and (Copy(S, Length(S), 1) = '"') then
    S := Trim(Copy(S, 2, Length(S) - 2));
  if (Length(S) >= 2) and (Copy(S, Length(S), 1) = '\') and (Copy(S, Length(S) - 1, 1) <> ':') then
    S := Copy(S, 1, Length(S) - 1);
  Result := S;
end;

function PathContainsDir(const PathValue, Dir: string): Boolean;
var
  Rest, Entry: string;
  P: Integer;
begin
  Result := False;
  Rest := PathValue;
  while Rest <> '' do
  begin
    P := Pos(';', Rest);
    if P = 0 then
    begin
      Entry := Rest;
      Rest := '';
    end
    else
    begin
      Entry := Copy(Rest, 1, P - 1);
      Rest := Copy(Rest, P + 1, Length(Rest) - P);
    end;
    if CompareText(NormalizePathEntry(Entry), NormalizePathEntry(Dir)) = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function RemoveDirFromPath(const PathValue, Dir: string): string;
var
  Rest, Entry: string;
  P: Integer;
begin
  Result := '';
  Rest := PathValue;
  while Rest <> '' do
  begin
    P := Pos(';', Rest);
    if P = 0 then
    begin
      Entry := Rest;
      Rest := '';
    end
    else
    begin
      Entry := Copy(Rest, 1, P - 1);
      Rest := Copy(Rest, P + 1, Length(Rest) - P);
    end;
    if CompareText(NormalizePathEntry(Entry), NormalizePathEntry(Dir)) <> 0 then
    begin
      if Result <> '' then
        Result := Result + ';';
      Result := Result + Trim(Entry);
    end;
  end;
end;

procedure AddUserPathDir(const Dir: string);
var
  Current: string;
begin
  if not RegQueryStringValue(HKCU, UserEnvKey, 'Path', Current) then
    Current := '';
  if PathContainsDir(Current, Dir) then
    Exit;
  if (Current <> '') and (Copy(Current, Length(Current), 1) <> ';') then
    Current := Current + ';';
  RegWriteExpandStringValue(HKCU, UserEnvKey, 'Path', Current + Dir);
end;

procedure RemoveUserPathDir(const Dir: string);
var
  Current, Updated: string;
begin
  if not RegQueryStringValue(HKCU, UserEnvKey, 'Path', Current) then
    Exit;
  Updated := RemoveDirFromPath(Current, Dir);
  if CompareText(Updated, Current) <> 0 then
    RegWriteExpandStringValue(HKCU, UserEnvKey, 'Path', Updated);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddUserPathDir(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    RemoveUserPathDir(ExpandConstant('{app}'));
    if DeleteBrowserEngine then
      DelTree(BrowserEngineDir(), True, True, True);
  end;
end;
