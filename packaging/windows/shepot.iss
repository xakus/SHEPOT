; Установщик SHEPOT для Windows (Inno Setup 6).
; Сборка: ISCC.exe /DAppVersion=0.1.0 packaging\windows\shepot.iss
; Ставится для текущего пользователя (без прав администратора) в
; %LOCALAPPDATA%\Programs\SHEPOT, ярлык в «Пуск», по желанию — автозапуск.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B0F3E0A-5C1D-4E7B-9A51-5E4F0B3C2D11}
AppName=SHEPOT
AppVersion={#AppVersion}
AppPublisher=xakus
AppPublisherURL=https://github.com/xakus/SHEPOT
DefaultDirName={localappdata}\Programs\SHEPOT
DefaultGroupName=SHEPOT
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\release
OutputBaseFilename=SHEPOT-Setup-{#AppVersion}
SetupIconFile=..\icons\generated\shepot.ico
UninstallDisplayIcon={app}\SHEPOT.exe
Compression=lzma2/fast
SolidCompression=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Запускать SHEPOT при входе в Windows"; Flags: checkedonce
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "..\..\dist\SHEPOT\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SHEPOT"; Filename: "{app}\SHEPOT.exe"
Name: "{group}\Удалить SHEPOT"; Filename: "{uninstallexe}"
Name: "{userdesktop}\SHEPOT"; Filename: "{app}\SHEPOT.exe"; Tasks: desktopicon
Name: "{userstartup}\SHEPOT"; Filename: "{app}\SHEPOT.exe"; Tasks: autostart

[Run]
Filename: "{app}\SHEPOT.exe"; Description: "{cm:LaunchProgram,SHEPOT}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/IM SHEPOT.exe /F"; Flags: runhidden; RunOnceId: "KillShepot"
