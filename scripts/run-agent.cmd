@echo off
REM ---------------------------------------------------------------------------
REM Telegram agent 的啟動腳本。工作排程器開機時呼叫它，也可以手動雙擊執行。
REM
REM 路徑用 %~dp0 從腳本自身位置推導，不碰 %LOCALAPPDATA%：
REM 排程工作用 S4U（不論是否登入都執行）時是非互動登入，環境變數不保證齊全，
REM 少了那個變數整個腳本會安靜地什麼都不做 —— 排查起來毫無線索。
REM
REM -X utf8 與 -u 都是必要的，不是裝飾：
REM   -X utf8  Windows 的 Python 預設用系統 ANSI 字碼頁寫 stdout，而這專案的
REM            log 全是中文，少了它第一行就會 UnicodeEncodeError 然後程序死掉。
REM   -u       輸出導向檔案時 stderr 會做區塊緩衝，少了它 log 會一直是空的。
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
set "LOG=%LOGDIR%\agent.log"

echo [%TIME%] starting agent (APP=%APP%) >> "%LOG%"


"%APP%\venv\Scripts\python.exe" -u -X utf8 "%APP%\telegram_adapter.py" >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo [%TIME%] agent exited with %RC% >> "%LOG%"

REM 設定錯誤（缺 token / 白名單 / 金鑰）回 2，重啟一萬次也不會變好，
REM 所以原樣往外丟，讓排程器的重啟設定決定要不要再試。
exit /b %RC%
