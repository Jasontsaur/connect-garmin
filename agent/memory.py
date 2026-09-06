"""
對話記憶 —— 每個 Telegram chat 一條時間軸，存在既有的 SQLite 檔裡。

只存「使用者說了什麼」與「agent 最後回了什麼」，中間的 tool_use / tool_result
一律不落地。理由是那些區塊必須成對出現（一個 tool_use 配一個 tool_result），
一旦裁切歷史時切在中間，下次送出去就會被 API 打回 400。工具結果本來就是
當下那一輪的暫時性資料，重跑一次比修好配對邏輯划算。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# 一個 chat 最多帶多少「則」訊息進 context（user + assistant 各算一則）。
# 20 則約等於 10 個來回，對這種查數據的對話夠用，也不會讓 token 一直漲。
HISTORY_LIMIT = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_chat
    ON agent_memory(chat_id, id DESC);
"""


class Memory:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(SCHEMA)

    def history(self, chat_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
        """
        取最近的對話，舊到新排列。

        第一則一定是 user —— API 規定 messages[0] 必須是 user，而由新往回取
        剛好可能切在 assistant 上，所以取完要把開頭的 assistant 丟掉。
        """
        rows = self.conn.execute(
            "SELECT role, content FROM agent_memory "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()

        messages = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        return messages

    def append(self, chat_id: int, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO agent_memory (chat_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                chat_id,
                role,
                content,
                datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def clear(self, chat_id: int) -> int:
        """清掉某個 chat 的歷史，回傳刪了幾則（給 /reset 用）。"""
        cur = self.conn.execute("DELETE FROM agent_memory WHERE chat_id = ?", (chat_id,))
        self.conn.commit()
        return cur.rowcount
