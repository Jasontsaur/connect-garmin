# garmin-endurance

每日兩次抓取 Garmin Connect 耐力分數，存進 SQLite，跑在 mini PC 的 WSL2 上。

## 檔案

| 檔案 | 用途 |
|---|---|
| `garmin_endurance.py` | 主程式，含 `login` / `fetch` / `backfill` / `merge` / `report` / `probe` 六個子命令 |
| `garmin-endurance.service` | systemd oneshot service |
| `garmin-endurance.timer` | 排程 07:20 與 19:20，各帶 ±10 分鐘隨機延遲 |
| `telegram_adapter.py` | Telegram 對話介面（long polling） |
| `agent/core.py` | Agent Core：串 LLM、工具、記憶 |
| `agent/tools.py` | 把既有能力包成 Claude 可呼叫的工具 |
| `agent/memory.py` | 每個 chat 的對話歷史 |
| `garmin-agent.service` | Telegram agent 的常駐 service |
| `install.sh` | 建 venv、裝套件、佈署 unit、產生 `~/.local/bin/garmin-endurance` |

## 部署

```bash
chmod +x install.sh && ./install.sh

nano ~/.config/garmin-endurance/env     # 填 GARMIN_EMAIL / GARMIN_PASSWORD
garmin-endurance login                  # 首次登入，會問 MFA
garmin-endurance fetch -v               # 試跑
garmin-endurance report

systemctl --user enable --now garmin-endurance.timer
```

登入成功後把 `env` 裡的 `GARMIN_PASSWORD` 清空即可 —— token 存在
`~/.garminconnect/garmin_tokens.json`，每次請求前會自動判斷是否需要 refresh，
只要 refresh token 沒失效就不必再輸入密碼。

## 幾個設計上的取捨

**日期區間參數是裝飾用的。** 這點很反直覺，值得記下來：
`calendarStartDate` / `calendarEndDate` 給多長的區間都沒用，端點一律只回
「今天」的單一快照，`aggregation` 換成 `weekly` / `monthly` 也一樣。
唯一能取得歷史值的問法是 `calendarDate=<單一日期>`，所以 `backfill`
只能一天打一次請求。

也因為這樣，`fetch` 的 `--days` 其實不影響拿到多少資料 ——
資料是每天跑兩次慢慢累積出來的，不是回溯抓的。

**這個帳號的資料邊界在 2023 年 8 月中。** 探過了：2023-08-01 無資料、
2023-08-20 有（5218），再往前到 2023-06 / 2022 / 2021 都是空的。
所以 `backfill` 起點設在 2023-07 以前只是白打請求。

**端點用試的，不是寫死。** 耐力分數在 `garminconnect` 裡沒有穩定的具名方法，
腳本按順序試三種 `metrics-service/metrics/endurancescore` 的參數形式，第一個
有回應的就用。三種都掛掉才算失敗。

**解析容錯。** Garmin 改欄位名是常態，所以解析器是遞迴走訪整包 JSON，找出同時
具備日期（`calendarDate` / `date` / `day`）與分數（`overallScore` /
`enduranceScore` / `score` / `value`）的節點，並額外處理 `groupMap` 這種
「日期在 key 上」的形狀。原始節點 JSON 一律存進 `raw_json` 欄位，之後要補算
別的指標不用重抓。

**寫入是冪等的。** 同一天的分數只有在數值或原始 JSON 真的變了才更新，並記錄
`updated_at`。所以一天跑兩次、回溯 45 天，不會製造重複資料 —— 反而剛好能觀察到
Garmin 事後修正歷史分數的情況。

**失敗不會靜默。** 每次執行都寫一筆 `fetch_log`，`report` 尾巴會顯示最後一次
的狀態與錯誤訊息。service 設 `Restart=on-failure` 搭 3 分鐘退避，最多三次。

## WSL2 的注意事項

- `/etc/wsl.conf` 要有 `[boot]` / `systemd=true`
- `sudo loginctl enable-linger $USER`，否則沒登入時 timer 不跑（你應該已經設過）
- WSL2 從 Windows 休眠喚醒後時鐘會漂移，`Persistent=true` 會補跑錯過的排程
- 時區確認：`timedatectl`，應該是 `Asia/Taipei`

## 常用指令

```bash
garmin-endurance backfill --days 365       # 回補一年歷史（逐日打，約 7 分鐘）
garmin-endurance backfill --start 2025-01-01 --end 2025-12-31
garmin-endurance report --days 60          # 趨勢 + 近 7 日均值比較
garmin-endurance probe                     # 印出端點原始 JSON（偵錯用）
systemctl --user list-timers garmin-endurance.timer
journalctl --user -u garmin-endurance.service -n 50
```

## intervals.icu 併表（`merge`）

在 `env` 填入 `ICU_API_KEY`（intervals.icu → Settings → Developer Settings）後：

```bash
garmin-endurance merge --days 60
garmin-endurance merge --days 180 --csv ~/pmc.csv
```

認證是 Basic auth，帳號固定字串 `API_KEY`、密碼是你的 key；`ICU_ATHLETE_ID`
留 `0` 就會由 key 自動解析，不必手動查 athlete id。

抓的是 `/athlete/{id}/wellness`，取 CTL、ATL、ramp rate、靜止心率、HRV、
睡眠與體重。欄位命名在 JSON 與 CSV 之間不一致（`ctl` vs `icu_ctl`），
所以跟 Garmin 那邊一樣用候選鍵取值，`raw_json` 也全存。

排程時 `merge` 接在 `fetch` 後面跑，unit 裡用 `ExecStart=-` 前綴 ——
intervals.icu 掛掉不會讓整個 unit 算失敗，Garmin 抓到的資料照樣保留。
同時會輸出一份 `~/.local/share/garmin-endurance/daily.csv`。

## 資料表

```sql
endurance_score(calendar_date PK, overall_score, classification,
                feedback_phrase, raw_json, first_seen_at, updated_at)

wellness(day PK, ctl, atl, ramp_rate, resting_hr, hrv,
         sleep_secs, weight, raw_json, updated_at)

fetch_log(id, ran_at, status, endpoint, rows_seen, rows_changed, message)
```

併好的檢視表 `v_daily`，直接查就有：

```sql
SELECT day, endurance, ctl, atl, tsb, acwr, resting_hr, hrv, sleep_hours
FROM v_daily WHERE day >= date('now','-90 day');
```

`tsb` 是 `ctl - atl`，`acwr` 是 `atl / ctl`。兩邊都可能缺日期（Garmin 不是每天
更新耐力分數），所以 view 先把日期聯集再各自 LEFT JOIN，不依賴 SQLite 3.39+
才有的 FULL OUTER JOIN。`v_daily` 每次開 DB 都會重建，改定義不用手動 migrate。

一個判讀上的提醒：ACWR 常被當成受傷風險指標，但近年的統合分析對它在個人層級的
預測力並不支持，當粗略參考就好，別當硬性門檻。

## Telegram agent

用自然語言問數據，不必 SSH 進來下指令。

```
Telegram
    │
    ▼
telegram_adapter.py     long polling、chat_id 白名單、訊息切段
    │
    ▼
agent/core.py           Agent Core
    │
    ├── LLM             OpenAI（型號由 OPENAI_MODEL 帶，不寫死）
    ├── Planner         自寫迴圈：呼叫工具 → 餵回結果 → 再問，上限 8 步
    ├── agent/memory.py 每個 chat 的對話歷史
    └── agent/tools.py  查數據 / 觸發抓取 / 看抓取健康度
```

### 啟用

在 `env` 補上三個變數後：

```bash
systemctl --user enable --now garmin-agent.service
journalctl --user -u garmin-agent.service -f
```

| 變數 | 說明 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 跟 [@BotFather](https://t.me/BotFather) 申請 |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 逗號分隔的白名單，**沒填會拒絕啟動** |
| `OPENAI_API_KEY` | OpenAI API 金鑰 |
| `OPENAI_MODEL` | 型號，留空用預設值 |

查自己的 chat id：先跟 bot 說一句話，再開
`https://api.telegram.org/bot<TOKEN>/getUpdates`。

### 幾個設計上的取捨

**白名單沒有預設值。** 少了它，任何搜到這個 bot 的人都能查你的靜止心率跟睡眠。
所以缺白名單時是直接 exit 2 拒絕啟動，而不是印個警告照跑。

**記憶只存結論，不存工具呼叫。** `tool_use` 與 `tool_result` 區塊必須成對出現，
歷史一旦裁切在中間，下次送出去就是 400。工具結果本來就是那一輪的暫時資料，
需要時重跑一次比維護配對邏輯划算。

**型號不寫死，而且啟動時驗證。** OpenAI 的型號命名換得比這個專案改版快。
`OPENAI_MODEL` 設錯時，agent 會在啟動時就報錯並列出這把金鑰實際可用的型號，
而不是等你在 Telegram 上傳第一則訊息才收到 404。列不出型號（例如走代理）
只警告不擋啟動。

**Planner 的迴圈有步數上限。** 模型鬼打牆時（一直要求同一個工具卻不下結論）
會在第 8 步停下來回一句話，不會無聲地一直燒 token。

**工具的錯誤是回給模型，不是往上炸。** 工具名打錯、參數不是合法 JSON、
執行時例外，一律包成一段 JSON 錯誤訊息當作工具結果餵回去 ——
模型收到還有機會換個做法，收不到就只會卡住。

**工具回 JSON 不回表格。** CLI 那些對齊過的 ASCII 表格是給人看的；
模型讀 JSON 更穩，也更省 token。

**工具直接重用既有函式。** 抓 Garmin 走 `cmd_fetch`，抓 intervals.icu 走
`fetch_wellness` / `upsert_wellness`，查數據走 `v_daily`。CLI 改了行為，
agent 這邊自動跟著改，不會有兩套會漂移的實作。

**會連外網的工具在 prompt 裡被壓著用。** 排程本來就每天跑兩次，
所以 `refresh_*` 只在使用者明確要求更新、或資料明顯過期時才呼叫。

## 想加別的指標

同一個 `metrics-service` 底下還有 `hillscore`；訓練狀態、HRV、VO2max 則有現成的
wrapper 方法（`get_training_status`、`get_hrv_data`、`get_max_metrics`）。
在 `cmd_fetch` 裡多加一段呼叫、多開一張表即可，解析器可以直接沿用。

## 免責

這是非官方介面。Garmin 在 2026 年 3 月改過一次認證流程，把整個 Python 生態系
打斷了一輪；之後還可能再改。別把關鍵流程綁死在上面，`raw_json` 全存就是為了
真的壞掉時還救得回來。
