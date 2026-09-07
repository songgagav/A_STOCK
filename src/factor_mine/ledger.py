# -*- coding: utf-8 -*-
"""候选因子台账: 登记(表达式/来源/说明) + 评估报告追加, JSONL 可追溯."""
import json
import os
import time

LEDGER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ledger.jsonl")


def register(name: str, expr: str, source: str = "manual", note: str = "") -> dict:
    cand = {"name": name, "expr": expr, "source": source, "note": note,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _append(cand)
    return cand


def append_report(name: str, report: dict) -> None:
    _append({"name": name, "type": "report", "report": report,
             "ts": time.strftime("%Y-%m-%d %H:%M:%S")})


def _append(obj: dict) -> None:
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load() -> list[dict]:
    if not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
