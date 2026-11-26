; Inno Setup 安装包。覆盖安装不会丢失授权：
; 授权状态在 %APPDATA%\ShootAtZombies（状态文件 + HKCU 注册表），不在 {app} 里。
; AppId 必须永远固定，否则会被识别成另一个软件并列安装。

#define MyAppName "向僵尸开炮助手"
; 每次发版**必须**递增；否则覆盖安装时 Inno Setup 无法区分新旧版本。
; 建议与发版清单一起核对，或由构建脚本注入。
#define MyAppVersion "1.0.0"
#define MyAppPublisher "ShootAtZombies"
#define MyAppExeName "ShootAtZombies.exe"

[Setup]
AppId={{A7B3C2D1-5E4F-4A6B-8C9D-0E1F2A3B4C5D}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\ShootAtZombies\app
DisableProgramGroupPage=yes
CloseApplications=yes
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=ShootAtZombiesSetup
Compression=lzma
SolidCompression=yes
VersionInfoVersion={#MyAppVersion}
WizardStyle=modern

[Languages]
; 默认英文消息文件保证任何 Inno Setup 安装都能编译；如需中文向导，
; 自行放置 ChineseSimplified.isl 后把下面一行改为对应文件。
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; 白名单：只收 exe 与 PyInstaller 的 _internal（含 templates），
; 绝不递归打包 dist 下其它文件，避免误把私钥/台账/调试日志收进 setup.exe。
; 只覆盖程序文件，绝不触碰 %APPDATA%\ShootAtZombies
Source: "..\dist\ShootAtZombies\ShootAtZombies.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\ShootAtZombies\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursedirs createallsubdirs

; 撤销令不随安装包自动下发：退款流程中由客户在激活窗口手动导入。
; 客户端也会扫描 {app}\revocations 与状态目录，但那需要作者自行投递文件，
; 本安装包不承诺该通道，避免误以为「放进目录就会自动作废」。

; 若干净机实测缺少 MSVC 运行库，取消下面两处注释并把 vc_redist.x64.exe 放到 packaging\
; Source: "vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
; Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; Flags: waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行"; Flags: nowait postinstall skipifsilent

; 覆盖安装前清掉旧版 _internal 残留（v1 有、v2 已删的 DLL/模板不会被留下）。
; 这里只动程序目录，绝不触碰 %APPDATA%\ShootAtZombies 的授权状态。
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[UninstallDelete]
; 故意留空：不要删除 {userappdata}\ShootAtZombies，卸载重装也要保留授权
