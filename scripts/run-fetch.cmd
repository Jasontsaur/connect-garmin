@echo off
REM ---------------------------------------------------------------------------
REM 每日兩次的抓取。對應原本 systemd 的 garmin-endurance.service。
REM
REM 路徑用 %~dp0 推導而非 %LOCALAPPDATA%，理由見 run-agent.cmd。
REM
REM merge 失敗不影響整體結果 —— intervals.icu 掛掉時，Garmin 那邊已經抓到的
REM 資料還是算成功。這對應原本 unit 裡 ExecStart 前面那個 "-" 前綴。
REM ---------------------------------------------------------------------------

REM 不要加 chcp。在 S4U（不論是否登入都執行）的排程工作下，整個批次會跑在
REM session 0、沒有主控台，chcp 會讓批次從那一行開始安靜中止 —— 不報錯、
REM 不寫 log、工作回報成功，排查起來毫無線索。花了幾輪才找到。
REM
REM 中文亂碼問題改用別的方式解：時間戳只用 %TIME%（純數字），不用 %DATE%
REM （zh-TW 的 %DATE% 含中文星期幾，走 OEM 字碼頁會變問號）。
REM Python 自己的 log 每行都有完整日期，所以這裡不重複。

setlocal
for %%I in ("%~dp0..") do set "APP=%%~fI"
set "LOGDIR=%APP%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\fetch.log"

echo [%TIME%] ---- fetch start (APP=%APP%) ---- >> "%LOG%"

"%APP%\venv\Scripts\python.exe" -u -X utf8 "%APP%\garmin_endurance.py" fetch >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

"%APP%\venv\Scripts\python.exe" -u -X utf8 "%APP%\garmin_endurance.py" merge --csv "%APP%\daily.csv" >> "%LOG%" 2>&1

REM 清掉 30 天前的日誌。沒有 logrotate 可用，不自己收就會一直長。
forfiles /p "%LOGDIR%" /m *.log /d -30 /c "cmd /c del @path" 2>nul

echo [%TIME%] ---- fetch done (rc=%RC%) ---- >> "%LOG%"
exit /b %RC%
