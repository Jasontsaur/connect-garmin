"""
Agent 層：把 garmin_endurance 的能力包成可以用自然語言驅動的 agent。

    telegram_adapter  →  core.AgentCore  →  ├── LLM（Claude）
                                            ├── Planner（tool runner）
                                            ├── memory.Memory
                                            └── tools.TOOLS

CLI（garmin_endurance.py）的行為完全不受影響，這裡只是另一個入口。
"""
