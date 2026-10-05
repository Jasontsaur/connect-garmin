# install.ps1 — 把 garmin-endurance 部署到 Windows（對應 Linux 的 install.sh）
#
# 用法（一般 PowerShell 視窗即可，不需要系統管理員）：
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# 設計上的三個重點：
#
# 1. 全部放在 %LOCALAPPDATA%。Local 不會漫遊、也不會被 OneDrive 同步 ——
#    這台機器的「文件」和「桌面」都已經被 OneDrive 接管，金鑰或健康資料
#    放進那些資料夾就會自動上傳到雲端。
#
# 2. 排程工作用 S4U 登入型別。這樣「不論使用者是否登入都執行」，
#    但 Windows 不需要保存你的密碼 —— 比起存密碼的作法安全得多，
#    代價只是沒有互動桌面（對背景服務來說無所謂）。
#
# 3. 每個 Python 呼叫都帶 -X utf8。Windows 的 Python 預設用系統 ANSI 字碼頁
#    寫 stdout，而這個專案的 log 全是中文，少了它第一行就會爆。

$ErrorActionPreference = "Stop"

$App     = Join-Path $env:LOCALAPPDATA "garmin-endurance"
$Src     = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path $App "env"
$Me      = "$env:USERDOMAIN\$env:USERNAME"

function Say($msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }

# --------------------------------------------------------------------------- #
Say "檢查 Python"

$Python = Get-Command python.exe -ErrorAction SilentlyContinue |
          Select-Object -ExpandProperty Source -First 1
if (-not $Python) {
    $guess = Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"
    if (Test-Path $guess) { $Python = $guess }
}
if (-not $Python) {
    throw "找不到 python.exe。請先安裝：winget install --id Python.Python.3.14 --scope user"
}

$ver = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "  $Python (Python $ver)"
$ok = & $Python -c "import sys; print(1 if sys.version_info >= (3,12) else 0)"
if ($ok -ne "1") { throw "需要 Python 3.12 以上（garminconnect 的要求），目前是 $ver" }

# --------------------------------------------------------------------------- #
Say "建立目錄並收緊權限"

New-Item -ItemType Directory -Force -Path $App, "$App\logs", "$App\tokens" | Out-Null

# 移除繼承來的授權，只留本人 / SYSTEM / Administrators。
# 誠實說明極限：SYSTEM 與系統管理員仍然讀得到，這跟 Linux 的 600 擋不住 root
# 是同一回事 —— 擋的是同機其他使用者，不是擋有管理權限的人。
icacls $App /inheritance:r /grant:r "${Me}:(OI)(CI)F" /grant:r "SYSTEM:(OI)(CI)F" /grant:r "Administrators:(OI)(CI)F" | Out-Null
Write-Host "  $App 已移除權限繼承"

# --------------------------------------------------------------------------- #
Say "建立虛擬環境並安裝套件"

if (-not (Test-Path "$App\venv")) { & $Python -m venv "$App\venv" }
& "$App\venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$App\venv\Scripts\python.exe" -m pip install --quiet --upgrade garminconnect curl_cffi openai
$pkgver = & "$App\venv\Scripts\python.exe" -m pip show garminconnect |
          Select-String '^Version' | ForEach-Object { $_.Line.Split(' ')[1] }
Write-Host "  garminconnect $pkgver"

# --------------------------------------------------------------------------- #
Say "複製程式"

New-Item -ItemType Directory -Force -Path "$App\agent", "$App\scripts" | Out-Null

# 部署目錄本身就是 git working copy 時（跟 Linux 那邊一樣的作法），
# Src 會等於 App —— 複製到自己身上會直接拋錯，所以要跳過。
if ((Resolve-Path $Src).Path -eq (Resolve-Path $App).Path) {
    Write-Host "  來源即部署目錄（git working copy），略過複製"
} else {
    Copy-Item "$Src\garmin_endurance.py", "$Src\telegram_adapter.py" $App -Force
    Copy-Item "$Src\agent\*.py"    "$App\agent"   -Force
    Copy-Item "$Src\scripts\*.cmd" "$App\scripts" -Force
    Write-Host "  已複製到 $App"
}

# --------------------------------------------------------------------------- #
Say "設定檔"

if (-not (Test-Path $EnvFile)) {
@"
# Garmin 憑證 —— 只有第一次 login 需要，之後靠 token 自動續期。
GARMIN_EMAIL=
GARMIN_PASSWORD=

GARMIN_BACKFILL_DAYS=45

# intervals.icu —— merge 子命令用。
ICU_API_KEY=
ICU_ATHLETE_ID=0

# Telegram agent。TELEGRAM_ALLOWED_CHAT_IDS 沒填 agent 會拒絕啟動
# （不然任何人找到這個 bot 都能查你的健康資料）。
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=

# OpenAI，agent 的大腦。
OPENAI_API_KEY=
OPENAI_MODEL=
"@ | Set-Content -Path $EnvFile -Encoding utf8NoBOM
    Write-Host "  已建立 $EnvFile"
} else {
    Write-Host "  $EnvFile 已存在，保留不動"
}

# 金鑰檔比目錄再緊一級：只有本人，連 Administrators 都不在 ACL 裡。
icacls $EnvFile /inheritance:r /grant:r "${Me}:F" | Out-Null
Write-Host "  已限制為僅本人可存取"

# --------------------------------------------------------------------------- #
Say "註冊排程工作"

$principal = New-ScheduledTaskPrincipal -UserId $Me -LogonType S4U -RunLevel Limited

# 每日兩次抓取。StartWhenAvailable 對應 systemd 的 Persistent=true：
# 機器關機錯過的那次，開機後會補跑。RandomDelay 對應 timer 的 ±10 分鐘。
$t1 = New-ScheduledTaskTrigger -Daily -At "07:20"
$t2 = New-ScheduledTaskTrigger -Daily -At "19:20"
$t1.RandomDelay = "PT10M"
$t2.RandomDelay = "PT10M"

$fetchSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "GarminEndurance-Fetch" `
    -Action (New-ScheduledTaskAction -Execute "$App\scripts\run-fetch.cmd") `
    -Trigger $t1, $t2 -Principal $principal -Settings $fetchSettings -Force | Out-Null
Write-Host "  GarminEndurance-Fetch（每日 07:20 / 19:20）"

# Telegram bot：開機就起，掛了每分鐘重試。ExecutionTimeLimit 0 = 不限時間，
# 否則排程器預設 3 天後會把常駐程序砍掉。
$agentSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "GarminEndurance-Agent" `
    -Action (New-ScheduledTaskAction -Execute "$App\scripts\run-agent.cmd") `
    -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Principal $principal -Settings $agentSettings -Force | Out-Null
Write-Host "  GarminEndurance-Agent（開機啟動，失敗每分鐘重試）"

# --------------------------------------------------------------------------- #
Say "完成。接下來："

@"

  1. 填入金鑰：
       notepad $EnvFile

     （用記事本沒關係 —— 程式自己讀這個檔，會把 CRLF 的 \`r 去掉）

  2. 首次登入 Garmin（會問 MFA，一定要在終端機手動跑）：
       & "$App\venv\Scripts\python.exe" -X utf8 "$App\garmin_endurance.py" login

  3. 試跑：
       & "$App\venv\Scripts\python.exe" -X utf8 "$App\garmin_endurance.py" fetch -v
       & "$App\venv\Scripts\python.exe" -X utf8 "$App\garmin_endurance.py" report

  4. 啟動 bot：
       Start-ScheduledTask -TaskName GarminEndurance-Agent
       Get-Content "$App\logs\agent.log" -Tail 20 -Wait

  查看排程狀態：
       Get-ScheduledTask -TaskName GarminEndurance-*
       Get-ScheduledTaskInfo -TaskName GarminEndurance-Fetch

"@ | Write-Host
