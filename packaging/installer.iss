; Inno Setup 安装包。覆盖安装不会丢失授权：
; 授权状态在 %APPDATA%\jiangshikaipao（状态文件 + HKCU 注册表），不在 {app} 里。
; AppId 必须永远固定，否则会被识别成另一个软件并列安装。

#define MyAppName "向僵尸开炮助手"
; 每次发版**必须**递增；否则覆盖安装时 Inno Setup 无法区分新旧版本。
; 建议与发版清单一起核对，或由构建脚本注入。
#define MyAppVersion "1.0.0"
#define MyAppPublisher "jiangshikaipao"
#define MyAppExeName "jiangshikaipao.exe"

[Setup]
AppId={{A7B3C2D1-5E4F-4A6B-8C9D-0E1F2A3B4C5D}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\jiangshikaipao\app
; 旧版装在带旧产品名的目录里。沿用上次路径会让升级后安装目录仍显示旧名字。
UsePreviousAppDir=no
DisableProgramGroupPage=yes
CloseApplications=yes
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=jiangshikaipaoSetup
Compression=lzma
SolidCompression=yes
VersionInfoVersion={#MyAppVersion}
WizardStyle=modern

[Languages]
; 官方安装包不一定带简体中文，语言文件随仓库走，两台电脑都能编出中文向导。
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[Files]
; 白名单：只收 exe 与 PyInstaller 的 _internal（含 templates），
; 绝不递归打包 dist 下其它文件，避免误把私钥/台账/调试日志收进 setup.exe。
; 只覆盖程序文件，绝不触碰 %APPDATA%\jiangshikaipao
Source: "..\dist\jiangshikaipao\jiangshikaipao.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\jiangshikaipao\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; 撤销令不随安装包自动下发：退款流程中由客户在激活窗口手动导入。
; 客户端也会扫描 {app}\revocations 与状态目录，但那需要作者自行投递文件，
; 本安装包不承诺该通道，避免误以为「放进目录就会自动作废」。

; 若干净机实测缺少 MSVC 运行库，取消下面两处注释并把 vc_redist.x64.exe 放到 packaging\
; Source: "vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
; 安装不提权。管理员账号下 {autodesktop} 会指向公共桌面，写不进去，快捷方式会被静默跳过。
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Run]
; Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; Flags: waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行"; Flags: nowait postinstall skipifsilent

; 覆盖安装前清掉旧版 _internal 残留（v1 有、v2 已删的 DLL/模板不会被留下）。
; 这里只动程序目录，绝不触碰 %APPDATA%\jiangshikaipao 的授权状态。
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
; 升级时清掉旧安装目录，避免本地应用数据里留下旧文件夹名。
Type: filesandordirs; Name: "{localappdata}\ShootAtZombies"

[UninstallDelete]
; 故意留空：不要删除 {userappdata}\jiangshikaipao，卸载重装也要保留授权
