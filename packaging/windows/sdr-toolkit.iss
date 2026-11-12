; Inno Setup script for SDR Toolkit. Built by .github/workflows/windows.yml:
;   iscc /DVersion=0.10.0 packaging\windows\sdr-toolkit.iss
; Installs for the current user (no administrator needed), so the app's own updates run silently,
; as SDR Tools Relay's do.

#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Source
  #define Source "..\..\dist\SDR Toolkit"
#endif

[Setup]
AppId={{A8385E57-46F8-4FD3-8B90-764CA27E5DDC}
AppName=SDR Toolkit
AppVersion={#Version}
AppPublisher=ssjrocks
AppPublisherURL=https://github.com/ssjrocks/sdr-toolkit
AppSupportURL=https://github.com/ssjrocks/sdr-toolkit/issues
DefaultDirName={localappdata}\Programs\SDR Toolkit
DefaultGroupName=SDR Toolkit
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=SDR-Toolkit-Setup-{#Version}
OutputDir=..\..\dist
SetupIconFile=..\..\build\icon.ico
UninstallDisplayIcon={app}\SDR Toolkit.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "{#Source}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
; Files of an older version that a new one no longer has would otherwise stay behind.
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{userprograms}\SDR Toolkit"; Filename: "{app}\SDR Toolkit.exe"
Name: "{userdesktop}\SDR Toolkit"; Filename: "{app}\SDR Toolkit.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\SDR Toolkit.exe"; Description: "Start SDR Toolkit"; Flags: postinstall nowait skipifsilent
; After a silent update (from the app), start the new version.
Filename: "{app}\SDR Toolkit.exe"; Flags: nowait; Check: LaunchAfterSilentInstall

[Code]
// /NOLAUNCH=1 skips that (the CI's test install, whose runner would wait for the app to exit).
function LaunchAfterSilentInstall: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:NOLAUNCH|0}') = '0');
end;
