"""
Agent 的工具層 —— 把 garmin_endurance 既有的能力包成 LLM 可以呼叫的函式。

這裡刻意不重寫任何抓取或解析邏輯：抓 Garmin 走 `cmd_fetch`，抓 intervals.icu 走
`fetch_wellness` / `upsert_wellness`，查數據走既有的 `v_daily` view。CLI 改了行為，
agent 這邊自動跟著改，不會有兩套會漂移的實作。

回傳一律是 JSON 字串。模型讀 JSON 比讀對齊過的表格穩，而且省 token ——
CLI 那些 ASCII 表格是給人看的，不是給模型看的。

schema 是手寫的，沒有從函式簽名自動產生。少數幾個工具而已，
自動產生要多一層依賴，而且工具描述是要調的東西（模型會照著它決定何時呼叫），
寫死在旁邊反而好改。
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path

# garmin_endurance.py 在上一層，跑 `python -m agent...` 或用 systemd 起來時
# 工作目錄不一定對，所以明確把專案根目錄加進 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import garmin_endurance as ge  # noqa: E402


def query_daily_metrics(days: int = 30) -> str:
    conn = ge.open_db()
    try:
        start = (date.today() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT * FROM v_daily WHERE day >= ? ORDER BY day", (start,)
        ).fetchall()
        return json.dumps(
            {"rows": [dict(r) for r in rows], "count": len(rows)},
            ensure_ascii=False,
            default=str,
        )
    finally:
        conn.close()


def get_endurance_trend(days: int = 30) -> str:
    conn = ge.open_db()
    try:
        start = (date.today() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT day, endurance FROM v_daily "
            "WHERE day >= ? AND endurance IS NOT NULL ORDER BY day",
            (start,),
        ).fetchall()

        scores = [r["endurance"] for r in rows]
        result: dict = {
            "points": [{"day": r["day"], "endurance": r["endurance"]} for r in rows],
            "count": len(rows),
        }
        if scores:
            result["min"] = min(scores)
            result["max"] = max(scores)
            result["latest"] = {"day": rows[-1]["day"], "endurance": scores[-1]}
        if len(scores) >= 14:
            head, tail = scores[:7], scores[-7:]
            result["avg_first_7"] = round(sum(head) / 7, 1)
            result["avg_last_7"] = round(sum(tail) / 7, 1)
            result["change"] = round(sum(tail) / 7 - sum(head) / 7, 1)
        else:
            result["note"] = "有分數的天數不足 14 天，無法比較前後 7 日均值"
        return json.dumps(result, ensure_ascii=False)
    finally:
        conn.close()


def refresh_garmin_data(days: int = 45) -> str:
    code = ge.cmd_fetch(Namespace(days=days))

    conn = ge.open_db()
    try:
        row = conn.execute(
            "SELECT ran_at, status, rows_seen, rows_changed, message "
            "FROM fetch_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest = conn.execute(
            "SELECT calendar_date, overall_score FROM endurance_score "
            "ORDER BY calendar_date DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    return json.dumps(
        {
            "ok": code == 0,
            "last_run": dict(row) if row else None,
            "latest_score": dict(latest) if latest else None,
        },
        ensure_ascii=False,
        default=str,
    )


def refresh_intervals_data(days: int = 60) -> str:
    end = date.today()
    start = end - timedelta(days=days)
    try:
        records = ge.fetch_wellness(start.isoformat(), end.isoformat())
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(exc)[:500]}, ensure_ascii=False)

    conn = ge.open_db()
    try:
        changed = ge.upsert_wellness(conn, records)
        conn.execute(
            "INSERT INTO fetch_log (ran_at, status, endpoint, rows_seen, rows_changed) "
            "VALUES (?, 'ok', 'intervals.icu/wellness', ?, ?)",
            (ge.now_iso(), len(records), changed),
        )
        conn.commit()
    finally:
        conn.close()

    return json.dumps(
        {"ok": True, "rows_seen": len(records), "rows_changed": changed},
        ensure_ascii=False,
    )


def get_data_health(limit: int = 5) -> str:
    conn = ge.open_db()
    try:
        rows = conn.execute(
            "SELECT ran_at, status, endpoint, rows_seen, rows_changed, message "
            "FROM fetch_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        latest = conn.execute(
            "SELECT MAX(calendar_date) AS d FROM endurance_score"
        ).fetchone()
        wellness = conn.execute("SELECT MAX(day) AS d FROM wellness").fetchone()
    finally:
        conn.close()

    return json.dumps(
        {
            "recent_runs": [dict(r) for r in rows],
            "latest_endurance_date": latest["d"] if latest else None,
            "latest_wellness_date": wellness["d"] if wellness else None,
            "today": date.today().isoformat(),
        },
        ensure_ascii=False,
        default=str,
    )


# --------------------------------------------------------------------------- #
# 給模型看的定義
#
# 這是 Responses API 的格式：name / description / parameters 都在頂層。
# Chat Completions 的格式是包在 "function" 底下的巢狀結構，兩者不通用 ——
# 用錯會直接 400。
# --------------------------------------------------------------------------- #

SCHEMAS = [
    {
        "type": "function",
        "name": "query_daily_metrics",
        "description": (
            "查詢每日的耐力分數與訓練負荷指標（CTL/ATL/TSB/ACWR/靜止心率/HRV/"
            "睡眠/體重）。唯讀，只讀本機資料庫，不會連線到 Garmin。"
            "想看「目前狀況如何」「最近趨勢」「某天的數字」都用這個。"
            "endurance 是 Garmin 耐力分數；ctl 是長期負荷（體能）；"
            "atl 是短期負荷（疲勞）；tsb = ctl - atl，正值代表恢復；"
            "acwr = atl / ctl。Garmin 不是每天更新，endurance 可能是 null。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "往回查幾天，預設 30"}
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_endurance_trend",
        "description": (
            "看耐力分數的走勢，含近 7 日均值與區間起始 7 日均值的比較。唯讀。"
            "回答「有沒有進步」「最近掉很多嗎」這類問題時用這個，"
            "比自己把每日資料讀過去再心算可靠。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "往回看幾天，預設 30"}
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "refresh_garmin_data",
        "description": (
            "連線到 Garmin Connect 抓最新的耐力分數並寫入資料庫。會走網路，"
            "通常要幾秒。排程本來就每天跑兩次（07:20 / 19:20），"
            "所以只在使用者明確要求更新、或查到的資料明顯過期時才呼叫。"
            "抓完想知道結果，再呼叫 query_daily_metrics。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "回溯抓幾天，預設 45。冪等寫入，重抓不會產生重複",
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "refresh_intervals_data",
        "description": (
            "從 intervals.icu 抓 CTL/ATL 等 wellness 資料並併入資料庫。會走網路。"
            "跟 refresh_garmin_data 一樣，排程已經定期在跑，"
            "只在使用者明確要求更新時才呼叫。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "回溯抓幾天，預設 60"}
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_data_health",
        "description": (
            "查最近幾次抓取的執行紀錄，判斷資料是不是新鮮、排程有沒有在跑。"
            "使用者問「資料多久沒更新了」「排程還活著嗎」「怎麼都沒有新資料」"
            "時用這個。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "回傳最近幾筆紀錄，預設 5"}
            },
            "required": [],
        },
    },
]

REGISTRY = {
    "query_daily_metrics": query_daily_metrics,
    "get_endurance_trend": get_endurance_trend,
    "refresh_garmin_data": refresh_garmin_data,
    "refresh_intervals_data": refresh_intervals_data,
    "get_data_health": get_data_health,
}
