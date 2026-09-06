"""
Agent Core —— 把 LLM、規劃、記憶、工具接起來。

Planner 這一層是自己寫的迴圈：問模型 → 模型要求呼叫工具 → 執行 → 把結果餵回去
→ 再問一次，直到模型不再要求工具為止。迴圈有步數上限，避免模型鬼打牆時
一直燒 token。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3

import openai

from .memory import Memory
from .tools import REGISTRY, SCHEMAS

log = logging.getLogger(__name__)

# 模型不寫死。OpenAI 的型號換得比這個專案改版快，寫死只會變成過期的常數。
DEFAULT_MODEL = "gpt-4o"

# 一輪對話最多讓模型呼叫幾次工具。正常查詢一到三次就夠，
# 設 8 是留給「先看健康度、發現過期、去抓、再查一次」這種合理的連續動作。
MAX_STEPS = 8

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
    def __init__(
        self,
        conn: sqlite3.Connection,
        client: openai.OpenAI | None = None,
        model: str | None = None,
    ):
        self.memory = Memory(conn)
        self.client = client or openai.OpenAI()
        self.model = model or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL

    def handle(self, chat_id: int, user_text: str) -> str:
        """跑完一輪對話，回傳要送回 Telegram 的文字。"""
        messages: list[dict] = [{"role": "system", "content": SYSTEM}]
        messages += self.memory.history(chat_id)
        messages.append({"role": "user", "content": user_text})

        try:
            reply = self._run(messages)
        except openai.AuthenticationError:
            log.error("OPENAI_API_KEY 無效")
            return "設定有問題：OpenAI 金鑰無效，我先不亂猜了。"
        except openai.RateLimitError:
            log.warning("觸發 rate limit")
            return "太頻繁或額度用完了，等一下再問我一次。"
        except openai.APIStatusError as exc:
            log.error("API 錯誤 %s：%s", exc.status_code, exc.message)
            return f"OpenAI API 回了 {exc.status_code}，這次沒能回答。"
        except openai.APIConnectionError:
            log.error("連不上 OpenAI API")
            return "連不上 OpenAI API，可能是網路問題。"
        except Exception as exc:  # noqa: BLE001
            # 工具自己爆掉也會走到這裡。寧可回一句錯誤，也不要讓 bot 整個靜默。
            log.exception("處理訊息時發生未預期錯誤")
            return f"出錯了：{str(exc)[:300]}"

        if not reply:
            reply = "（沒有產生文字回覆）"

        # 只把最後的結論寫進記憶 —— 中間的工具呼叫不落地，原因見 memory.py
        self.memory.append(chat_id, "user", user_text)
        self.memory.append(chat_id, "assistant", reply)
        return reply

    def _run(self, messages: list[dict]) -> str:
        for step in range(MAX_STEPS):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=SCHEMAS,
            )
            message = response.choices[0].message

            if not message.tool_calls:
                return (message.content or "").strip()

            # 帶著 tool_calls 的 assistant 訊息一定要原樣放回去，
            # 否則下一輪的 tool 結果會對不到人。
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in message.tool_calls
                    ],
                }
            )

            for call in message.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": self._execute(call),
                    }
                )

        log.warning("連續呼叫工具 %d 次仍未收斂，中止", MAX_STEPS)
        return "查了幾輪還是繞不出來，換個方式問問看。"

    def _execute(self, call) -> str:
        """
        執行一個工具呼叫。任何失敗都要回一段文字給模型，不能讓例外往上炸 ——
        模型收到錯誤訊息還有機會換個做法，收不到就只會卡住。
        """
        name = call.function.name
        func = REGISTRY.get(name)
        if func is None:
            log.warning("模型要求了不存在的工具：%s", name)
            return json.dumps({"error": f"沒有這個工具：{name}"}, ensure_ascii=False)

        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError as exc:
            log.warning("工具 %s 的參數不是合法 JSON：%s", name, exc)
            return json.dumps({"error": f"參數解析失敗：{exc}"}, ensure_ascii=False)

        log.info("呼叫工具 %s %s", name, args)
        try:
            return func(**args)
        except TypeError as exc:
            log.warning("工具 %s 的參數不對：%s", name, exc)
            return json.dumps({"error": f"參數不正確：{exc}"}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            log.exception("工具 %s 執行失敗", name)
            return json.dumps({"error": str(exc)[:500]}, ensure_ascii=False)

    def reset(self, chat_id: int) -> str:
        n = self.memory.clear(chat_id)
        return f"已清掉 {n} 則對話紀錄，重新開始。"


def api_key_present() -> bool:
    """給啟動時的檢查用：沒有金鑰就早點喊，不要等使用者傳訊息才失敗。"""
    return bool(os.getenv("OPENAI_API_KEY"))


def verify_model(client: openai.OpenAI, model: str) -> tuple[bool, str]:
    """
    啟動時確認金鑰能用、而且設定的型號真的存在。

    型號打錯是很容易犯的錯（OpenAI 的命名一直在變），與其等使用者傳第一則訊息
    才收到 404，不如啟動時就講清楚，並把帳號實際可用的型號列出來。
    """
    try:
        available = {m.id for m in client.models.list()}
    except openai.AuthenticationError:
        return False, "OPENAI_API_KEY 無效"
    except Exception as exc:  # noqa: BLE001
        # 列不出型號不代表不能用（有些代理不支援 /models），只警告不擋啟動
        return True, f"無法列出型號（{str(exc)[:200]}），略過驗證"

    if model in available:
        return True, f"使用型號 {model}"

    hint = sorted(m for m in available if m.startswith(("gpt", "o1", "o3", "o4")))
    return False, (
        f"型號 {model} 不在這把金鑰可用的清單裡。"
        f"可用的有：{', '.join(hint[:20]) or '（清單是空的）'}"
    )
