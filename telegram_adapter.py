#!/usr/bin/env python3
"""
Telegram Adapter —— agent 的對外介面。

用 long polling 而不是 webhook：這台機器在 NAT 後面，沒有固定公開網址，
要架 webhook 得多一層反向代理跟憑證，對一個自己用的 bot 不划算。

環境變數：
  TELEGRAM_BOT_TOKEN         跟 @BotFather 申請的 token
  TELEGRAM_ALLOWED_CHAT_IDS  逗號分隔的 chat id 白名單（必填）
  OPENAI_API_KEY             OpenAI API 金鑰
  OPENAI_MODEL               型號，不填用 agent.core 的預設值
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import garmin_endurance as ge  # noqa: E402
from agent.core import AgentCore, api_key_present, verify_model  # noqa: E402

log = logging.getLogger("telegram")

API_BASE = "https://api.telegram.org/bot{token}/{method}"

# long polling 的等待秒數。Telegram 端最多 50，取 30 讓每次連線都有機會
# 在 systemd 重啟或網路變動時乾淨收尾。
POLL_TIMEOUT = 30

# Telegram 單則訊息上限 4096 字元，留一點餘裕。
MAX_MESSAGE = 3900

_running = True


def _stop(signum, frame) -> None:
    global _running
    log.info("收到訊號 %s，收工", signum)
    _running = False


class Telegram:
    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, method: str, params: dict, timeout: int = 20) -> dict:
        url = API_BASE.format(token=self.token, method=method)
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def get_updates(self, offset: int | None) -> list[dict]:
        params = {"timeout": POLL_TIMEOUT}
        if offset is not None:
            params["offset"] = offset
        # 讀取逾時要比 long poll 本身長，否則每次都會在還沒收到東西前就斷線
        result = self.call("getUpdates", params, timeout=POLL_TIMEOUT + 15)
        return result.get("result", [])

    def send(self, chat_id: int, text: str) -> None:
        """送訊息。超過長度上限就切成多則，切在換行處以免把句子腰斬。"""
        for chunk in _split(text, MAX_MESSAGE):
            self.call("sendMessage", {"chat_id": chat_id, "text": chunk})

    def typing(self, chat_id: int) -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:  # noqa: BLE001
            pass  # 純裝飾，失敗不值得中斷流程


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current)
            current = ""
        # 單行就超過上限的極端狀況，硬切
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current += line
    if current:
        chunks.append(current)
    return chunks


HELP = """\
直接用中文問我訓練數據就好，例如：
  最近狀況如何？
  這個月耐力分數有進步嗎？
  幫我抓最新的資料
  資料多久沒更新了？

指令：
  /reset  清掉對話記憶，重新開始
  /help   這則說明
"""


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("沒有 TELEGRAM_BOT_TOKEN，請在 ~/.config/garmin-endurance/env 補上")
        return 2

    raw_ids = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw_ids:
        # 沒有白名單就等於把自己的健康資料開放給任何找到這個 bot 的人。
        # 這種預設值不該存在，所以直接拒絕啟動。
        log.error("沒有 TELEGRAM_ALLOWED_CHAT_IDS，拒絕在無白名單的狀態下啟動")
        return 2
    allowed = {int(x) for x in raw_ids.replace(" ", "").split(",") if x}

    if not api_key_present():
        log.error("沒有 OPENAI_API_KEY，agent 無法運作")
        return 2

    tg = Telegram(token)
    conn = ge.open_db()
    agent = AgentCore(conn)

    ok, detail = verify_model(agent.client, agent.model)
    if not ok:
        log.error("%s", detail)
        return 2
    log.info("%s", detail)

    log.info("啟動完成，白名單 %s", sorted(allowed))

    offset: int | None = None
    backoff = 1

    while _running:
        try:
            updates = tg.get_updates(offset)
            backoff = 1
        except urllib.error.URLError as exc:
            log.warning("拉取更新失敗（%s），%d 秒後重試", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("拉取更新時發生非預期錯誤（%s），%d 秒後重試", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue

            chat_id = message["chat"]["id"]
            text = (message.get("text") or "").strip()
            if not text:
                continue

            if chat_id not in allowed:
                log.warning("拒絕未授權的 chat_id %s：%.50s", chat_id, text)
                continue

            log.info("[%s] %s", chat_id, text)
            # 模型加上工具呼叫可能跑好幾秒，先讓對方看到「輸入中」
            tg.typing(chat_id)
            try:
                reply = _dispatch(agent, chat_id, text)
            except Exception:  # noqa: BLE001
                log.exception("處理訊息失敗")
                reply = "處理時出錯了，log 裡有細節。"

            try:
                tg.send(chat_id, reply)
            except Exception:  # noqa: BLE001
                log.exception("送出回覆失敗")

    conn.close()
    log.info("已停止")
    return 0


def _dispatch(agent: AgentCore, chat_id: int, text: str) -> str:
    if text in ("/start", "/help"):
        return HELP
    if text == "/reset":
        return agent.reset(chat_id)
    return agent.handle(chat_id, text)


if __name__ == "__main__":
    raise SystemExit(main())
