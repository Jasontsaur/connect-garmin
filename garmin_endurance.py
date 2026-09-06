#!/usr/bin/env python3
"""
garmin_endurance.py — 定期抓取 Garmin Connect 耐力分數（Endurance Score）並存入 SQLite。

子命令：
  login    首次互動式登入（處理 MFA），把 token 存到 tokenstore
  fetch    非互動抓取（給 systemd timer 用），寫入 SQLite
  merge    抓 intervals.icu 的 CTL/ATL，與耐力分數併成同一張表
  report   印出最近的耐力分數趨勢
  probe    列印各候選端點的原始 JSON，用來偵錯 / 確認欄位結構

環境變數：
  GARMIN_EMAIL           Garmin 帳號（僅 login 需要）
  GARMIN_PASSWORD        Garmin 密碼（僅 login 需要）
  GARMIN_TOKENS          token 目錄，預設 ~/.garminconnect
  GARMIN_DB              SQLite 路徑，預設 ~/.local/share/garmin-endurance/garmin.db
  GARMIN_BACKFILL_DAYS   每次抓取回溯天數，預設 45
  ICU_API_KEY            intervals.icu API key（Settings → Developer Settings）
  ICU_ATHLETE_ID         athlete id，預設 0（由 API key 自動解析）
"""

from __future__ import annotations

import argparse
import base64
import csv
import fcntl
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from garminconnect import Garmin
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少套件，請先執行： pip install --upgrade garminconnect curl_cffi\n")
    raise SystemExit(2)


# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #

TOKENSTORE = os.path.expanduser(os.getenv("GARMIN_TOKENS", "~/.garminconnect"))
DB_PATH = Path(
    os.path.expanduser(
        os.getenv("GARMIN_DB", "~/.local/share/garmin-endurance/garmin.db")
    )
)
BACKFILL_DAYS = int(os.getenv("GARMIN_BACKFILL_DAYS", "45"))
LOCK_PATH = Path(os.path.expanduser("~/.cache/garmin-endurance.lock"))

# 耐力分數目前沒有穩定的具名 wrapper 方法，直接打原始端點。
# Garmin 偶爾會調整路徑，所以按順序試，第一個成功的就用。
#
# query 參數必須跟路徑分開傳：garminconnect 0.3.x 起會擋掉路徑裡含 "?" 的請求
# （client._run_request 的 path 驗證），所以這裡存 (path, params) 而不是完整 URL。
ENDPOINT_PATH = "/metrics-service/metrics/endurancescore"
ENDPOINT_TEMPLATES = [
    (ENDPOINT_PATH, {"calendarStartDate": "{start}", "calendarEndDate": "{end}",
                     "aggregation": "daily"}),
    (ENDPOINT_PATH, {"startDate": "{start}", "endDate": "{end}",
                     "aggregation": "daily"}),
    (ENDPOINT_PATH, {"calendarDate": "{end}"}),
]


def build_params(tmpl: dict, start: str, end: str) -> dict:
    """把 params 模板裡的 {start}/{end} 佔位符填實。"""
    return {k: v.format(start=start, end=end) for k, v in tmpl.items()}


def describe(path: str, params: dict) -> str:
    """給日誌／錯誤訊息用的可讀表示。"""
    return path + "?" + "&".join(f"{k}={v}" for k, v in params.items())

# 在回傳 JSON 中辨識分數欄位時，依序嘗試這些鍵名
SCORE_KEYS = ("overallScore", "enduranceScore", "score", "value")
DATE_KEYS = ("calendarDate", "date", "day")

# --- intervals.icu ---------------------------------------------------------- #
ICU_BASE = "https://intervals.icu/api/v1"
ICU_API_KEY = os.getenv("ICU_API_KEY", "")
ICU_ATHLETE_ID = os.getenv("ICU_ATHLETE_ID", "0")  # 0 = 由 API key 自動解析

# intervals.icu 的 wellness 欄位在 JSON 與 CSV 之間命名不一致（ctl vs icu_ctl），
# 一律用候選清單取值。
WELLNESS_FIELDS: dict[str, tuple[str, ...]] = {
    "ctl": ("ctl", "icu_ctl", "fitness"),
    "atl": ("atl", "icu_atl", "fatigue"),
    "ramp_rate": ("rampRate", "ramp_rate", "icu_ramp_rate"),
    "resting_hr": ("restingHR", "restingHr", "resting_hr"),
    "hrv": ("hrv", "icu_hrv"),
    "sleep_secs": ("sleepSecs", "sleep_secs"),
    "weight": ("weight",),
}
WELLNESS_DATE_KEYS = ("id", "date", "day", "calendarDate")

log = logging.getLogger("garmin-endurance")


# --------------------------------------------------------------------------- #
# 資料庫
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS endurance_score (
    calendar_date   TEXT PRIMARY KEY,
    overall_score   INTEGER,
    classification  INTEGER,
    feedback_phrase TEXT,
    raw_json        TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at       TEXT NOT NULL,
    status       TEXT NOT NULL,
    endpoint     TEXT,
    rows_seen    INTEGER DEFAULT 0,
    rows_changed INTEGER DEFAULT 0,
    message      TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetch_log_ran_at ON fetch_log(ran_at DESC);

CREATE TABLE IF NOT EXISTS wellness (
    day         TEXT PRIMARY KEY,
    ctl         REAL,
    atl         REAL,
    ramp_rate   REAL,
    resting_hr  INTEGER,
    hrv         REAL,
    sleep_secs  INTEGER,
    weight      REAL,
    raw_json    TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 兩邊都可能缺日期，所以先把日期聯集起來再各自 LEFT JOIN，
-- 避免依賴 SQLite 3.39+ 才有的 FULL OUTER JOIN。
DROP VIEW IF EXISTS v_daily;
CREATE VIEW v_daily AS
WITH days AS (
    SELECT calendar_date AS day FROM endurance_score
    UNION
    SELECT day FROM wellness
)
SELECT
    d.day                                   AS day,
    e.overall_score                         AS endurance,
    w.ctl                                   AS ctl,
    w.atl                                   AS atl,
    ROUND(w.ctl - w.atl, 1)                 AS tsb,
    CASE WHEN w.ctl > 0 THEN ROUND(w.atl / w.ctl, 2) END AS acwr,
    w.ramp_rate                             AS ramp_rate,
    w.resting_hr                            AS resting_hr,
    w.hrv                                   AS hrv,
    ROUND(w.sleep_secs / 3600.0, 1)         AS sleep_hours,
    w.weight                                AS weight
FROM days d
LEFT JOIN endurance_score e ON e.calendar_date = d.day
LEFT JOIN wellness        w ON w.day           = d.day
ORDER BY d.day;
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 登入
# --------------------------------------------------------------------------- #

def build_client(interactive: bool) -> Garmin:
    """
    非互動模式：只用既有 token 續期，失敗就直接報錯（避免在 timer 裡卡在 MFA 輸入）。
    互動模式：用帳密登入，需要時提示 MFA 驗證碼。
    """
    if interactive:
        email = os.getenv("GARMIN_EMAIL")
        password = os.getenv("GARMIN_PASSWORD")
        if not email or not password:
            raise SystemExit(
                "login 需要 GARMIN_EMAIL / GARMIN_PASSWORD 環境變數，"
                "請確認已 source 你的 env 檔。"
            )
        client = Garmin(email, password, prompt_mfa=lambda: input("MFA 驗證碼: "))
        client.login(TOKENSTORE)
        return client

    def _refuse_mfa() -> str:
        raise RuntimeError(
            "Garmin 要求 MFA，但目前是非互動執行。"
            "請在終端機手動跑一次： garmin-endurance login"
        )

    client = Garmin(prompt_mfa=_refuse_mfa)
    client.login(TOKENSTORE)  # 讀既有 token，必要時自動 refresh
    return client


# --------------------------------------------------------------------------- #
# 解析：容錯地從 JSON 裡挖出 (日期, 分數) 
# --------------------------------------------------------------------------- #

def _walk(node: Any) -> Iterable[dict]:
    """遞迴走訪所有 dict 節點。"""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def extract_records(payload: Any) -> list[dict]:
    """
    從任意結構的回應中，找出同時具備『日期』與『分數』的節點。
    Garmin 改欄位名時不會整包壞掉，只要 raw_json 有存就能事後補救。
    """
    found: dict[str, dict] = {}

    for node in _walk(payload):
        d = next((node[k] for k in DATE_KEYS if isinstance(node.get(k), str)), None)
        s = next(
            (node[k] for k in SCORE_KEYS if isinstance(node.get(k), (int, float))), None
        )
        if not d or s is None:
            continue
        d = d[:10]
        if len(d) != 10 or d[4] != "-":
            continue
        found[d] = {
            "calendar_date": d,
            "overall_score": int(round(float(s))),
            "classification": node.get("classification"),
            "feedback_phrase": node.get("feedbackPhrase") or node.get("feedback"),
            "raw_json": json.dumps(node, ensure_ascii=False, sort_keys=True),
        }

    # groupMap 形式：{"2026-08-01": {...}} — 日期在 key 上而不在節點裡
    if isinstance(payload, dict):
        for container_key in ("groupMap", "scoreMap", "dailyMap"):
            group = payload.get(container_key)
            if not isinstance(group, dict):
                continue
            for day, node in group.items():
                if not isinstance(day, str) or len(day) < 10:
                    continue
                day = day[:10]
                if day in found:
                    continue
                s = next(
                    (
                        n[k]
                        for n in _walk(node)
                        for k in SCORE_KEYS
                        if isinstance(n.get(k), (int, float))
                    ),
                    None,
                )
                if s is None:
                    continue
                found[day] = {
                    "calendar_date": day,
                    "overall_score": int(round(float(s))),
                    "classification": None,
                    "feedback_phrase": None,
                    "raw_json": json.dumps(node, ensure_ascii=False, sort_keys=True),
                }

    return sorted(found.values(), key=lambda r: r["calendar_date"])


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #

def call_with_retry(client: Garmin, path: str, params: dict, attempts: int = 3) -> Any:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return client.connectapi(path, params=params)
        except Exception as exc:  # noqa: BLE001 — 上游例外型別不穩定
            last = exc
            wait = 2 ** i * 5
            log.warning("請求失敗（第 %d/%d 次）：%s，%d 秒後重試", i + 1, attempts, exc, wait)
            time.sleep(wait)
    raise last  # type: ignore[misc]


def fetch_endurance(client: Garmin, start: str, end: str) -> tuple[str, Any]:
    errors = []
    for path, ptmpl in ENDPOINT_TEMPLATES:
        params = build_params(ptmpl, start, end)
        label = describe(path, params)
        try:
            data = call_with_retry(client, path, params)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label} -> {exc}")
            continue
        if data:
            log.info("端點成功：%s", path)
            return label, data
        errors.append(f"{label} -> 空回應")
    raise RuntimeError("所有候選端點都失敗：\n  " + "\n  ".join(errors))


def upsert(conn: sqlite3.Connection, records: list[dict]) -> int:
    changed = 0
    ts = now_iso()
    for r in records:
        cur = conn.execute(
            "SELECT overall_score, raw_json FROM endurance_score WHERE calendar_date = ?",
            (r["calendar_date"],),
        )
        row = cur.fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO endurance_score
                   (calendar_date, overall_score, classification, feedback_phrase,
                    raw_json, first_seen_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["calendar_date"],
                    r["overall_score"],
                    r["classification"],
                    r["feedback_phrase"],
                    r["raw_json"],
                    ts,
                    ts,
                ),
            )
            changed += 1
        elif row["overall_score"] != r["overall_score"] or row["raw_json"] != r["raw_json"]:
            conn.execute(
                """UPDATE endurance_score
                   SET overall_score = ?, classification = ?, feedback_phrase = ?,
                       raw_json = ?, updated_at = ?
                   WHERE calendar_date = ?""",
                (
                    r["overall_score"],
                    r["classification"],
                    r["feedback_phrase"],
                    r["raw_json"],
                    ts,
                    r["calendar_date"],
                ),
            )
            changed += 1
    conn.commit()
    return changed


def cmd_fetch(args: argparse.Namespace) -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("已有另一個抓取程序在執行，本次略過")
        return 0

    conn = open_db()
    end = date.today()
    start = end - timedelta(days=args.days)
    endpoint = None
    try:
        client = build_client(interactive=False)
        endpoint, payload = fetch_endurance(client, start.isoformat(), end.isoformat())
        records = extract_records(payload)
        if not records:
            raise RuntimeError(
                "端點有回應但解析不到分數欄位，"
                "請跑 `garmin-endurance probe` 看原始結構。"
            )
        changed = upsert(conn, records)
        conn.execute(
            """INSERT INTO fetch_log (ran_at, status, endpoint, rows_seen, rows_changed)
               VALUES (?, 'ok', ?, ?, ?)""",
            (now_iso(), endpoint.split("?")[0], len(records), changed),
        )
        conn.commit()
        latest = records[-1]
        log.info(
            "完成：%d 天資料，%d 筆新增/更新；最新 %s = %s",
            len(records),
            changed,
            latest["calendar_date"],
            latest["overall_score"],
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("抓取失敗：%s", exc)
        conn.execute(
            """INSERT INTO fetch_log (ran_at, status, endpoint, message)
               VALUES (?, 'error', ?, ?)""",
            (now_iso(), endpoint, str(exc)[:2000]),
        )
        conn.commit()
        return 1
    finally:
        conn.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


# --------------------------------------------------------------------------- #
# 回補歷史
# --------------------------------------------------------------------------- #

def fetch_one_day(client: Garmin, day: str) -> dict | None:
    """
    抓單一天的分數。

    為什麼要一天打一次：這個端點的 calendarStartDate/calendarEndDate 是裝飾用的，
    不論給多長的區間都只回「今天」的單一快照，aggregation 換成 weekly/monthly
    也一樣。只有 calendarDate=<單日> 這個問法會回傳該日的歷史值。
    """
    payload = call_with_retry(client, ENDPOINT_PATH, {"calendarDate": day})
    for rec in extract_records(payload):
        # 只認日期相符的那筆。沒有資料的日子，Garmin 可能回最近一次的快照，
        # 照單全收會把今天的分數蓋到過去的日期上。
        if rec["calendar_date"] == day:
            return rec
    return None


def cmd_backfill(args: argparse.Namespace) -> int:
    # 跟排程的 fetch 共用同一把鎖，避免兩邊同時寫入
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("已有另一個抓取程序在執行（可能是排程），稍後再試")
        return 1

    conn = open_db()
    try:
        today = date.today()
        if args.start:
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end) if args.end else today
        else:
            end = today
            start = end - timedelta(days=args.days)
        end = min(end, today)  # 未來的日期沒有意義

        if start > end:
            log.error("起始日期 %s 晚於結束日期 %s", start, end)
            return 1

        existing = {
            r[0]
            for r in conn.execute(
                "SELECT calendar_date FROM endurance_score WHERE calendar_date BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            )
        }

        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        todo = days if args.force else [d for d in days if d.isoformat() not in existing]

        log.info(
            "回補 %s ~ %s：共 %d 天，已有 %d 天，這次要打 %d 個請求（約 %.0f 分鐘）",
            start, end, len(days), len(existing), len(todo),
            len(todo) * (args.sleep + 0.4) / 60,
        )
        if not todo:
            log.info("沒有需要回補的日期")
            return 0

        client = build_client(interactive=False)
        got = changed = missing = 0

        try:
            for i, day in enumerate(todo, 1):
                iso = day.isoformat()
                try:
                    rec = fetch_one_day(client, iso)
                except Exception as exc:  # noqa: BLE001
                    # 單日失敗不該讓整趟回補中斷 —— 記下來，繼續往下跑
                    log.warning("%s 抓取失敗：%s", iso, str(exc)[:200])
                    continue

                if rec is None:
                    missing += 1
                else:
                    got += 1
                    changed += upsert(conn, [rec])

                if i % 30 == 0 or i == len(todo):
                    conn.commit()
                    log.info(
                        "進度 %d/%d（%s）：有分數 %d 天、無資料 %d 天、寫入 %d 筆",
                        i, len(todo), iso, got, missing, changed,
                    )

                if i < len(todo):
                    time.sleep(args.sleep)
        except KeyboardInterrupt:
            # 可續跑，所以中斷不算災難：把已完成的存好再退出
            conn.commit()
            log.warning("使用者中斷。已寫入的保留，重跑會從沒抓到的日期接著補")
            return 130

        conn.commit()
        conn.execute(
            """INSERT INTO fetch_log (ran_at, status, endpoint, rows_seen, rows_changed, message)
               VALUES (?, 'ok', ?, ?, ?, ?)""",
            (
                now_iso(),
                ENDPOINT_PATH + "?calendarDate（逐日回補）",
                got,
                changed,
                f"{start} ~ {end}，無資料 {missing} 天",
            ),
        )
        conn.commit()
        log.info(
            "回補完成：有分數 %d 天、無資料 %d 天、新增/更新 %d 筆", got, missing, changed
        )
        return 0
    finally:
        conn.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


# --------------------------------------------------------------------------- #
# intervals.icu
# --------------------------------------------------------------------------- #

def icu_get(path: str, attempts: int = 3) -> Any:
    """
    intervals.icu 用 Basic auth，帳號固定字串 API_KEY，密碼是你的 key。
    只用 stdlib，不額外拉 requests 進來。
    """
    if not ICU_API_KEY:
        raise RuntimeError(
            "未設定 ICU_API_KEY。到 intervals.icu → Settings → Developer Settings "
            "產生 key，再寫進 ~/.config/garmin-endurance/env"
        )
    token = base64.b64encode(f"API_KEY:{ICU_API_KEY}".encode()).decode()
    req = urllib.request.Request(
        ICU_BASE + path,
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": "garmin-endurance/1.0",
        },
    )
    last: Exception | None = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (401, 403):
                raise RuntimeError(f"intervals.icu 認證失敗（{exc.code}）：{body}") from exc
            if exc.code == 404:
                raise RuntimeError(
                    f"找不到資源（404）。確認 ICU_ATHLETE_ID 正確，或設為 0 自動解析。{body}"
                ) from exc
            last = exc
        except Exception as exc:  # noqa: BLE001
            last = exc
        wait = 2 ** i * 5
        log.warning("intervals.icu 請求失敗（第 %d/%d 次）：%s，%d 秒後重試",
                    i + 1, attempts, last, wait)
        time.sleep(wait)
    raise RuntimeError(f"intervals.icu 請求持續失敗：{last}")


def _pick(node: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = node.get(k)
        if v is not None:
            return v
    return None


def fetch_wellness(start: str, end: str) -> list[dict]:
    path = f"/athlete/{ICU_ATHLETE_ID}/wellness?oldest={start}&newest={end}"
    data = icu_get(path)
    if isinstance(data, dict):  # 少數情況會包一層
        data = data.get("wellness") or data.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError(f"wellness 回應格式非預期：{type(data).__name__}")

    out = []
    for node in data:
        if not isinstance(node, dict):
            continue
        day = _pick(node, WELLNESS_DATE_KEYS)
        if not isinstance(day, str) or len(day) < 10:
            continue
        rec = {"day": day[:10], "raw_json": json.dumps(node, ensure_ascii=False,
                                                       sort_keys=True)}
        for col, keys in WELLNESS_FIELDS.items():
            rec[col] = _pick(node, keys)
        out.append(rec)
    return sorted(out, key=lambda r: r["day"])


def upsert_wellness(conn: sqlite3.Connection, records: list[dict]) -> int:
    ts = now_iso()
    cols = list(WELLNESS_FIELDS)
    changed = 0
    for r in records:
        existing = conn.execute(
            "SELECT raw_json FROM wellness WHERE day = ?", (r["day"],)
        ).fetchone()
        if existing and existing["raw_json"] == r["raw_json"]:
            continue
        conn.execute(
            f"""INSERT INTO wellness (day, {', '.join(cols)}, raw_json, updated_at)
                VALUES (?{', ?' * (len(cols) + 2)})
                ON CONFLICT(day) DO UPDATE SET
                  {', '.join(f'{c} = excluded.{c}' for c in cols)},
                  raw_json = excluded.raw_json,
                  updated_at = excluded.updated_at""",
            [r["day"]] + [r[c] for c in cols] + [r["raw_json"], ts],
        )
        changed += 1
    conn.commit()
    return changed


def _fmt(v: Any, spec: str = "") -> str:
    if v is None:
        return "—"
    return format(v, spec) if spec else str(v)


def cmd_merge(args: argparse.Namespace) -> int:
    conn = open_db()
    end = date.today()
    start = end - timedelta(days=args.days)

    try:
        records = fetch_wellness(start.isoformat(), end.isoformat())
    except Exception as exc:  # noqa: BLE001
        log.error("%s", exc)
        conn.execute(
            "INSERT INTO fetch_log (ran_at, status, endpoint, message) "
            "VALUES (?, 'error', 'intervals.icu/wellness', ?)",
            (now_iso(), str(exc)[:2000]),
        )
        conn.commit()
        conn.close()
        return 1

    changed = upsert_wellness(conn, records)
    conn.execute(
        """INSERT INTO fetch_log (ran_at, status, endpoint, rows_seen, rows_changed)
           VALUES (?, 'ok', 'intervals.icu/wellness', ?, ?)""",
        (now_iso(), len(records), changed),
    )
    conn.commit()
    log.info("intervals.icu：%d 天資料，%d 筆新增/更新", len(records), changed)

    rows = conn.execute(
        "SELECT * FROM v_daily WHERE day >= ? ORDER BY day", (start.isoformat(),)
    ).fetchall()

    if args.csv:
        out = Path(os.path.expanduser(args.csv))
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(rows[0].keys() if rows else [])
            w.writerows([tuple(r) for r in rows])
        print(f"已匯出 {len(rows)} 列 → {out}")
        conn.close()
        return 0

    print(f"\n耐力分數 × PMC（{start} ~ {end}）\n")
    print("  日期          耐力    CTL    ATL    TSB   ACWR   RHR    HRV")
    print("  " + "─" * 60)
    for r in rows:
        print(
            f"  {r['day']}  {_fmt(r['endurance']):>6} "
            f"{_fmt(r['ctl'], '.1f'):>6} {_fmt(r['atl'], '.1f'):>6} "
            f"{_fmt(r['tsb'], '.1f'):>6} {_fmt(r['acwr'], '.2f'):>6} "
            f"{_fmt(r['resting_hr']):>5} {_fmt(r['hrv'], '.1f'):>6}"
        )

    gap = sum(1 for r in rows if r["endurance"] is None)
    if gap:
        print(f"\n  註：{gap} 天沒有耐力分數（Garmin 不是每天都會更新）")

    scored = [r for r in rows if r["endurance"] is not None and r["ctl"] is not None]
    if len(scored) >= 14:
        half = len(scored) // 2
        d_e = scored[-1]["endurance"] - scored[half]["endurance"]
        d_c = scored[-1]["ctl"] - scored[half]["ctl"]
        print(f"\n  後半段變化：耐力 {d_e:+d}，CTL {d_c:+.1f}")

    conn.close()
    return 0


# --------------------------------------------------------------------------- #
# 其他子命令
# --------------------------------------------------------------------------- #

def cmd_login(args: argparse.Namespace) -> int:
    client = build_client(interactive=True)
    name = client.get_full_name()
    print(f"登入成功：{name}")
    print(f"Token 已存於 {TOKENSTORE}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    conn = open_db()
    rows = conn.execute(
        """SELECT calendar_date, overall_score, classification, feedback_phrase
           FROM endurance_score ORDER BY calendar_date DESC LIMIT ?""",
        (args.days,),
    ).fetchall()
    if not rows:
        print("資料庫還沒有資料，先跑一次 fetch。")
        return 1

    rows = list(reversed(rows))
    scores = [r["overall_score"] for r in rows]
    lo, hi = min(scores), max(scores)
    span = max(hi - lo, 1)

    print(f"\n耐力分數（最近 {len(rows)} 天）  範圍 {lo} – {hi}\n")
    prev = None
    for r in rows:
        bar = "█" * (1 + int(28 * (r["overall_score"] - lo) / span))
        delta = "" if prev is None else f"{r['overall_score'] - prev:+d}"
        print(f"  {r['calendar_date']}  {r['overall_score']:>6}  {delta:>6}  {bar}")
        prev = r["overall_score"]

    if len(scores) >= 8:
        recent = sum(scores[-7:]) / 7
        earlier = sum(scores[:7]) / 7
        print(f"\n  近 7 日均值 {recent:.0f}，區間起始 7 日均值 {earlier:.0f}"
              f"（{recent - earlier:+.0f}）")

    last = conn.execute(
        "SELECT ran_at, status, message FROM fetch_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last:
        tail = f" — {last['message'][:80]}" if last["message"] else ""
        print(f"\n  最後一次抓取：{last['ran_at']} [{last['status']}]{tail}")
    conn.close()
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    client = build_client(interactive=False)
    end = date.today()
    start = end - timedelta(days=args.days)
    for path, ptmpl in ENDPOINT_TEMPLATES:
        params = build_params(ptmpl, start.isoformat(), end.isoformat())
        print(f"\n=== {describe(path, params)}")
        try:
            data = client.connectapi(path, params=params)
        except Exception as exc:  # noqa: BLE001
            print(f"  失敗：{exc}")
            continue
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
        print(f"  --> 解析出 {len(extract_records(data))} 筆")
    return 0


# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(description="Garmin 耐力分數抓取工具")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="互動式登入並儲存 token").set_defaults(func=cmd_login)

    f = sub.add_parser("fetch", help="抓取並寫入資料庫")
    f.add_argument("--days", type=int, default=BACKFILL_DAYS)
    f.set_defaults(func=cmd_fetch)

    b = sub.add_parser("backfill", help="逐日回補歷史耐力分數")
    b.add_argument("--days", type=int, default=365, help="從今天往回幾天，預設 365")
    b.add_argument("--start", metavar="YYYY-MM-DD", help="指定起始日，會蓋掉 --days")
    b.add_argument("--end", metavar="YYYY-MM-DD", help="指定結束日，預設今天")
    b.add_argument("--sleep", type=float, default=0.8, help="每個請求之間停幾秒，預設 0.8")
    b.add_argument("--force", action="store_true", help="連已經有的日期也重抓")
    b.set_defaults(func=cmd_backfill)

    m = sub.add_parser("merge", help="併入 intervals.icu 的 CTL/ATL")
    m.add_argument("--days", type=int, default=60)
    m.add_argument("--csv", metavar="PATH", help="改為匯出 CSV 而非印出表格")
    m.set_defaults(func=cmd_merge)

    r = sub.add_parser("report", help="顯示趨勢")
    r.add_argument("--days", type=int, default=30)
    r.set_defaults(func=cmd_report)

    pr = sub.add_parser("probe", help="偵錯：印出端點原始回應")
    pr.add_argument("--days", type=int, default=14)
    pr.set_defaults(func=cmd_probe)

    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
