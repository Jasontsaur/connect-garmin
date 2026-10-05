@echo off
REM cmd 自己的輸出（%DATE% 的星期幾）走 OEM 字碼頁，不切 UTF-8 會變問號
chcp 65001 >nul
REM ---------------------------------------------------------------------------
REM Telegram agent 的啟動腳本。工作排程器開機時呼叫它，也可以手動雙擊執行。
REM
REM -X utf8 是必要的，不是裝飾：Windows 的 Python 預設用系統 ANSI 字碼頁
REM (繁中環境是 cp950) 寫 stdout，而這個專案的 log 訊息全是中文 ——
REM 少了它第一行 log 就會 UnicodeEncodeError 然後整個程序死掉。
REM ---------------------------------------------------------------------------

set "APP=%LOCALAPPDATA%\garmin-endurance"
set "LOGDIR=%APP%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo [%DATE% %TIME%] starting agent >> "%LOGDIR%\agent.log"
"%APP%\venv\Scripts\python.exe" -u -X utf8 "%APP%\telegram_adapter.py" >> "%LOGDIR%\agent.log" 2>&1
set RC=%ERRORLEVEL%
echo [%DATE% %TIME%] agent exited with %RC% >> "%LOGDIR%\agent.log"

REM 設定錯誤（缺 token / 白名單 / 金鑰）回 2，重啟一萬次也不會變好，
REM 所以原樣往外丟，讓排程器的重啟設定決定要不要再試。
exit /b %RC%
