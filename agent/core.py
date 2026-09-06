"""
Agent Core —— 把 LLM、規劃、記憶、工具接起來。

規劃（Planner）這一層沒有自己寫迴圈：用 SDK 的 tool runner，它會自動處理
「模型要求呼叫工具 → 執行 → 把結果餵回去 → 再問一次」直到模型不再要求工具為止。
自己手寫那個 while 迴圈只會多一份要維護的狀態機。
"""

from __future__ import annotations

import logging
import os
import sqlite3

import anthropic

from .memory import Memory
from .tools import TOOLS

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
MAX_TOKENS = 16000

SYSTEM = """\
你是一個跑在使用者自己 mini PC 上的耐力訓練數據助理，透過 Telegram 對話。
資料來源是使用者的 Garmin Connect（耐力分數）與 intervals.icu（訓練負荷），
都已經抓進本機的 SQLite。

工作方式：
- 需要數字就呼叫工具，不要憑印象回答，也不要自己編造沒查到的日期或分數。
- 唯讀查詢（query_daily_metrics、get_endurance_trend、get_data_health）隨時可以用。
- 會連外網的工具（refresh_garmin_data、refresh_intervals_data）只在使用者明確
  要求更新、或發現資料明顯過期時才用；排程本來就每天自動跑兩次。
- 查不到資料就說查不到，並用 get_data_health 看看是不是抓取失敗了。

回答風格：
- 這是 Telegram，訊息要短。除非使用者要求細節，控制在幾行以內。
- 用繁體中文。不要用 Markdown 表格（手機上會亂掉），需要列數字就用簡單的條列。
- 直接講結論，不要覆述使用者的問題，也不要每次都解釋你呼叫了哪些工具。

判讀上的注意事項：
- TSB = CTL - ATL。正值代表恢復中，負值代表疲勞累積。
- Garmin 不是每天更新耐力分數，中間有空缺是正常的，不要當成資料異常。
- 單日的耐力分數波動很大，看趨勢比看單點有意義。
- ACWR 常被當成受傷風險指標，但近年的統合分析並不支持它在個人層級的預測力。
  可以提，但不要當成硬性門檻，更不要據此叫使用者停練。
- 你不是醫療專業。可以解讀訓練數據，但不要診斷或給醫療建議；
  數據出現異常時，建議使用者找專業人士看，不要自己下判斷。
"""


class AgentCore:
    def __init__(self, conn: sqlite3.Connection, client: anthropic.Anthropic | None = None):
        self.memory = Memory(conn)
        self.client = client or anthropic.Anthropic()

    def handle(self, chat_id: int, user_text: str) -> str:
        """跑完一輪對話，回傳要送回 Telegram 的文字。"""
        messages = self.memory.history(chat_id)
        messages.append({"role": "user", "content": user_text})

        try:
            final = self._run(messages)
        except anthropic.AuthenticationError:
            log.error("ANTHROPIC_API_KEY 無效或未設定")
            return "設定有問題：Claude API 金鑰無效，我先不亂猜了。"
        except anthropic.RateLimitError:
            log.warning("觸發 rate limit")
            return "太頻繁了，等一下再問我一次。"
        except anthropic.APIStatusError as exc:
            log.error("API 錯誤 %s：%s", exc.status_code, exc.message)
            return f"Claude API 回了 {exc.status_code}，這次沒能回答。"
        except anthropic.APIConnectionError:
            log.error("連不上 Claude API")
            return "連不上 Claude API，可能是網路問題。"
        except Exception as exc:  # noqa: BLE001
            # 工具自己爆掉也會走到這裡。寧可回一句錯誤，也不要讓 bot 整個靜默。
            log.exception("處理訊息時發生未預期錯誤")
            return f"出錯了：{str(exc)[:300]}"

        if final is None:
            return "沒有拿到回應，再試一次看看。"

        if final.stop_reason == "refusal":
            log.warning("模型拒絕回答：%s", final.stop_details)
            return "這個問題我不能回答。"

        reply = "\n".join(b.text for b in final.content if b.type == "text").strip()
        if not reply:
            reply = "（沒有產生文字回覆）"

        # 只把最後的結論寫進記憶 —— 中間的工具呼叫不落地，原因見 memory.py
        self.memory.append(chat_id, "user", user_text)
        self.memory.append(chat_id, "assistant", reply)
        return reply

    def _run(self, messages: list[dict]):
        runner = self.client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
        )
        final = None
        for message in runner:
            final = message
            for block in message.content:
                if block.type == "tool_use":
                    log.info("呼叫工具 %s %s", block.name, block.input)
        return final

    def reset(self, chat_id: int) -> str:
        n = self.memory.clear(chat_id)
        return f"已清掉 {n} 則對話紀錄，重新開始。"


def api_key_present() -> bool:
    """給啟動時的檢查用：沒有金鑰就早點喊，不要等使用者傳訊息才失敗。"""
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
