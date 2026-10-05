@echo off
REM ---------------------------------------------------------------------------
REM 每日兩次的抓取。對應原本 systemd 的 garmin-endurance.service。
REM
REM merge 失敗不影響整體結果 —— intervals.icu 掛掉時，Garmin 那邊已經抓到的
REM 資料還是算成功。這對應原本 unit 裡 ExecStart 前面那個 "-" 前綴。
REM ---------------------------------------------------------------------------

set "APP=%LOCALAPPDATA%\garmin-endurance"
set "LOGDIR=%APP%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\fetch.log"

echo [%DATE% %TIME%] ---- fetch start ---- >> "%LOG%"
"%APP%\venv\Scripts\python.exe" -X utf8 "%APP%\garmin_endurance.py" fetch >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

"%APP%\venv\Scripts\python.exe" -X utf8 "%APP%\garmin_endurance.py" merge --csv "%APP%\daily.csv" >> "%LOG%" 2>&1

REM 清掉 30 天前的日誌。沒有 logrotate 可用，不自己收就會一直長。
forfiles /p "%LOGDIR%" /m *.log /d -30 /c "cmd /c del @path" 2>nul

echo [%DATE% %TIME%] ---- fetch done (rc=%RC%) ---- >> "%LOG%"
exit /b %RC%
