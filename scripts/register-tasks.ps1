# register-tasks.ps1 — 註冊兩個排程工作
#
# 必須用系統管理員身分執行。原因是 S4U 登入型別：
# 「不論使用者是否登入都執行」需要 SeTcbPrivilege，一般使用者註冊會被拒。
#
# 為什麼堅持用 S4U 而不是存密碼的 Password 型別：S4U 同樣能在未登入時執行，
# 但 Windows 不需要保存你的密碼。安全性較好，代價只是沒有互動桌面 ——
# 對背景服務來說無所謂。
#
# 用法（在系統管理員 PowerShell 裡）：
#   powershell -ExecutionPolicy Bypass -File .\register-tasks.ps1

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$App = Join-Path $env:LOCALAPPDATA "garmin-endurance"
$Log = Join-Path $App "logs\register-tasks.log"

# 以系統管理員執行時 $env:LOCALAPPDATA 仍是本人的（不是 Administrator 的），
# 因為 UAC 提權不換使用者設定檔。但還是確認一下，免得路徑指到別人家。
if (-not (Test-Path "$App\scripts\run-agent.cmd")) {
    throw "找不到 $App\scripts\run-agent.cmd，請先跑 install.ps1"
}

$Me = "$env:USERDOMAIN\$env:USERNAME"
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

# Telegram bot：開機就起，掛了每分鐘重試。ExecutionTimeLimit 0 = 不限時間，
# 否則排程器預設 3 天後會把常駐程序砍掉。
#
# 注意這裡直接執行 python.exe，不像 fetch 那樣經過 .cmd ——
# 夾一層 cmd.exe 的話，排程器管到的是 cmd，停止工作時 python 會變成孤兒
# 繼續輪詢 Telegram；等工作再啟動就有兩個 poller 搶同一個 token，
# 症狀是訊息隨機消失。日誌改由 telegram_adapter.py 自己寫檔案。
$agentSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew

# 註冊前先清掉殘留的 bot 程序，避免新舊兩個同時輪詢。
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*telegram_adapter.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Register-ScheduledTask -TaskName "GarminEndurance-Agent" `
    -Action (New-ScheduledTaskAction `
        -Execute "$App\venv\Scripts\python.exe" `
        -Argument "-u -X utf8 `"$App\telegram_adapter.py`"" `
        -WorkingDirectory $App) `
    -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Principal $principal -Settings $agentSettings -Force | Out-Null

$result = Get-ScheduledTask -TaskName "GarminEndurance-*" |
          ForEach-Object { "{0}  state={1}  logon={2}" -f $_.TaskName, $_.State, $_.Principal.LogonType }
$result | Tee-Object -FilePath $Log
"OK $(Get-Date -Format s)" | Add-Content $Log
