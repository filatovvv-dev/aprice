; ============================================================================
; АвтоПрайс (APrice) — скрипт установщика для Inno Setup 6 (подзадача 6.1)
; Сборка: python build.py (сам найдёт ISCC.exe) либо вручную:
;   ISCC.exe build_scripts\installer.iss
; Перед этим должна быть выполнена сборка PyInstaller (dist\ с тремя exe и _internal).
; Файл сохранён в UTF-8 с BOM — иначе Inno Setup прочитает кириллицу неверно.
; ============================================================================

#define MyAppName "АвтоПрайс"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "APrice"
#define ClientExe "APrice-Client.exe"
#define BuilderExe "APrice-Builder.exe"
#define ServerExe "APrice-Server.exe"

[Setup]
; Постоянный идентификатор приложения (не менять: по нему работают обновление и удаление).
AppId={{6F2C8A1E-4B7D-4E5A-9C31-2A7D5E0B8F64}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\APrice
DefaultGroupName={#MyAppName}
OutputDir=..\dist
OutputBaseFilename=APrice_Setup_v{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
SetupIconFile=assets\icon_client.ico
UninstallDisplayIcon={app}\{#ClientExe}
UninstallDisplayName={#MyAppName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
; Установка в Program Files требует прав администратора.
PrivilegesRequired=admin

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на &Рабочем столе"; GroupDescription: "Дополнительные значки:"

[Dirs]
; Программы пишут config, logs, data и browser (Chromium) рядом с exe. В Program Files
; обычному пользователю писать нельзя, поэтому выдаём группе «Пользователи» право записи.
Name: "{app}\config"; Permissions: users-modify
Name: "{app}\logs"; Permissions: users-modify
Name: "{app}\data"; Permissions: users-modify
Name: "{app}\browser"; Permissions: users-modify

[Files]
; Всё содержимое dist\: три exe и общий _internal. Рабочие каталоги разработчика и
; сам установщик в пакет не попадают.
Source: "..\dist\*"; DestDir: "{app}"; Excludes: "config\*,logs\*,data\*,browser\*,APrice_Setup*.exe"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "assets\icon_client.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "assets\icon_builder.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "assets\icon_server.ico"; DestDir: "{app}\assets"; Flags: ignoreversion

[Icons]
; Рабочий стол
Name: "{autodesktop}\АвтоПрайс — Оценка авто"; Filename: "{app}\{#ClientExe}"; IconFilename: "{app}\assets\icon_client.ico"; Tasks: desktopicon
; Меню «Пуск»
Name: "{group}\АвтоПрайс — Оценка авто"; Filename: "{app}\{#ClientExe}"; IconFilename: "{app}\assets\icon_client.ico"
Name: "{group}\АвтоПрайс — Обновление справочников"; Filename: "{app}\{#BuilderExe}"; IconFilename: "{app}\assets\icon_builder.ico"
Name: "{group}\Удалить АвтоПрайс"; Filename: "{uninstallexe}"

[Run]
; Необязательный пункт в конце установки. Client сам запускает Server (5.6).
Filename: "{app}\{#ClientExe}"; Description: "Запустить АвтоПрайс"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallDelete]
; Chromium (~150 МБ) и журналы удаляем; config и data (настройки, история, база) сохраняются.
Type: filesandordirs; Name: "{app}\browser"
Type: filesandordirs; Name: "{app}\logs"
