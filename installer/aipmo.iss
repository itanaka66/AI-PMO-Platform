; AI-PMO Platform ― Inno Setup script
;
; これをコンパイルすると単一の .exe インストーラができる。
; Compiling this produces a single .exe installer.
;
; 前提 / Prerequisites:
;   1. installer/build.ps1 を実行して dist\aipmo\ を作る
;      Run installer/build.ps1 first to produce dist\aipmo\
;   2. Inno Setup 6 / https://jrsoftware.org/isinfo.php
;
; コンパイル / Compile:
;   iscc installer\aipmo.iss
;
; 署名する場合 / To sign while compiling ― installer/build.ps1 does this for
; you automatically when a certificate is configured (see there); calling
; iscc directly needs both flags:
;   iscc /DSIGN /Saipmosign="signtool.exe sign ... $f" installer\aipmo.iss

#define AppName "AI-PMO Platform"
#define AppVersion "0.1.3"
#define AppPublisher "AI-PMO"
#define AppExeName "aipmo.exe"

[Setup]
AppId={{8F3A1C42-6B7D-4E19-9A25-3C0D8E5F7B14}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\AI-PMO
DefaultGroupName=AI-PMO
OutputDir=..\dist
OutputBaseFilename=AI-PMO-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; 管理者権限を求めない。社給 PC で権限が無い利用者が多いため。
; No administrator rights: many users on managed corporate machines lack them.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UninstallDisplayIcon={app}\{#AppExeName}

; SIGN が定義されているとき（build.ps1 が証明書を見つけたとき）だけ、
; インストーラ本体とアンインストーラに署名する。未定義のときは何もしない
; ので、証明書の無いビルドはこれまでどおり通る。
;
; Signs the installer itself and the uninstaller, but only when SIGN is
; defined (build.ps1 defines it once it has found a certificate). Left
; undefined, this does nothing, so a build with no certificate still compiles
; exactly as before.
#ifdef SIGN
SignTool=aipmosign
SignedUninstaller=yes
#endif

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "english";  MessagesFile: "compiler:Default.isl"

[CustomMessages]
japanese.LaunchSetup=初期設定を開始する
english.LaunchSetup=Run first-time setup
japanese.CreateDesktopIcon=デスクトップにショートカットを作成する
english.CreateDesktopIcon=Create a desktop shortcut

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"

[Files]
; PyInstaller の出力一式 / the PyInstaller bundle
Source: "..\dist\aipmo\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; データファイルはユーザーが編集するため、実行ファイルとは別に扱う。
; Data files are user-editable, so they are kept separate from the binary.
Source: "..\prompts\*";   DestDir: "{app}\prompts";   Flags: ignoreversion recursesubdirs
Source: "..\templates\*"; DestDir: "{app}\templates"; Flags: ignoreversion recursesubdirs
Source: "..\sql\*";       DestDir: "{app}\sql";       Flags: ignoreversion recursesubdirs
Source: "..\queries.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";    DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\AI-PMO";       Filename: "{app}\{#AppExeName}"; Parameters: "--help"
Name: "{group}\AI-PMO Setup"; Filename: "{app}\{#AppExeName}"; Parameters: "setup"
Name: "{autodesktop}\AI-PMO"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "setup"; \
    Description: "{cm:LaunchSetup}"; Flags: postinstall skipifsilent

[UninstallDelete]
; 設定と鍵は消さない。再インストール時に入力し直させないため。
; Config and credentials are preserved so a reinstall does not force re-entry.
Type: filesandordirs; Name: "{app}\__pycache__"
