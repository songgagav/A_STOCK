# -*- coding: utf-8 -*-
"""三层 Kill Switch（路线图 #1）：GLOBAL / ACCOUNT / STRATEGY。

为什么需要它
------------
加固前，「停止交易」这件事散落成各处 `or` 判断：`realtime_engine.py:890` 是
`if self.pb.state == "DRAW_DOWN" or self._gate.get("freeze_new_buys"): return 0`。
能挡住买入，但**没有人工层**（发现数据被污染/策略上错线时，只能去改代码或杀进程）、
**没有留痕**（谁在何时因何停的手，事后查不出）、**没有持久**（重启即忘）。
这与 `P0-FREEZE-0925` 的欠缺同源：**缺少不可变快照 + 无留痕**。

三层语义（**谁拉闸 / 停什么**必须分清，否则一层出事会连带把别层的判断掩盖）
------------------------------------------------------------------------
  GLOBAL    人工层。运维/人在紧急时拉的总闸（数据污染的 09-04 类事故、上游故障、
            发现策略上错线）。**持久、显式解除、必须留痕**。只停**新开仓**。
  ACCOUNT   账户层。账户自身风险状态（组合熔断 DRAW_DOWN、单日重亏 loss_flag）。
            由 `PaperBook`/门控产出，不另造阈值。
  STRATEGY  策略层。策略健康度（IC 门控冻结、行情源冻住 —— 后者来自 #4 看门狗）。

**只停新开仓，绝不停离场**（重要）
----------------------------------
三层都**不拦止损/减仓**。把卖出也闸掉等于把风险锁在仓里（跌了也出不来），
比不设闸门更危险。故本模块只回答「能不能开新仓」。

失效方向（fail-safe）
---------------------
  · 开关文件**不存在** => 视为"没有人工闸门", 正常交易（否则首次部署即静默停手）
  · 开关文件**存在但不可解析/读不出** => **按最严处理: 当作已拉闸**（fail-closed）
    理由不对称: 忽略一次可能存在的紧急总闸（系统带病继续交易）, 后果远大于
    少做一天模拟盘; 且此时会**响亮报警**, 不会静默。
  · 未知字段一律忽略（向前兼容）。

留痕
----
所有拉闸/解除/拦截都追加进 `data/kill_switch_ledger.jsonl`（append-only，UTF-8），
含 ts / layer / action / actor / reason。**#6 哈希链审计**将来在此账本上续接，
故此处不做删除、不做重写。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

LAYERS = ("GLOBAL", "ACCOUNT", "STRATEGY")

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def state_path(path: str | None = None) -> str:
    return path or os.path.join(_repo_root(), "data", "kill_switch.json")


def ledger_path(path: str | None = None) -> str:
    return path or os.path.join(_repo_root(), "data", "kill_switch_ledger.jsonl")


# ------------------------------------------------------------------ 裁决(纯函数)

def evaluate(global_state: dict | None, account: dict | None = None,
             strategy: dict | None = None, global_unreadable: bool = False) -> dict:
    """三层合并成单一裁决（**纯函数**，CI 可测）。

    入参（缺 = 该层无信息，**不得**当作"已拉闸"）:
      global_state      {'engaged': bool, 'reason': str, 'actor': str, 'since': ts}
                        None 表示开关文件不存在（未拉闸）
      global_unreadable True 表示文件存在但不可解析 => fail-closed
      account           {'state': 'DRAW_DOWN'|..., 'reason': str|None}
      strategy          {'ic_freeze': bool, 'feed_stale': bool, 'reason': str|None}

    返回 {'blocked': bool, 'layers': [层名], 'reasons': [str], 'fail_closed': bool}
    """
    hits: list = []

    # GLOBAL —— 人工层
    if global_unreadable:
        hits.append(("GLOBAL", "开关状态文件存在但不可解析 => 按最严处理(fail-closed): "
                               "不静默继续交易, 请人工确认后解除"))
    elif isinstance(global_state, dict) and global_state.get("engaged"):
        r = str(global_state.get("reason") or "").strip() or "未注明原因"
        who = str(global_state.get("actor") or "?").strip()
        since = str(global_state.get("since") or "?").strip()
        hits.append(("GLOBAL", f"人工总闸已拉下({who} @{since}): {r}"))

    # ACCOUNT —— 账户层
    acc = account or {}
    if str(acc.get("state") or "").upper() == "DRAW_DOWN":
        hits.append(("ACCOUNT", str(acc.get("reason")
                                     or "组合熔断 DRAW_DOWN: 暂停加仓(仅止损/离场)")))

    # STRATEGY —— 策略层
    st = strategy or {}
    if st.get("ic_freeze"):
        hits.append(("STRATEGY", str(st.get("reason") or "IC 门控冻结: 暂停新买入")))
    if st.get("feed_stale"):
        hits.append(("STRATEGY", "行情源冻住(数据不再流动): 不得按陈旧价格开新仓"
                                 "（归因见 #4 flow_watchdog）"))

    return {
        "blocked": bool(hits),
        "layers": [h[0] for h in hits],
        "reasons": [h[1] for h in hits],
        "fail_closed": bool(global_unreadable),
    }


# ------------------------------------------------------------------ 状态读写

def read_global(path: str | None = None) -> dict:
    """读 GLOBAL 层开关（**不抛异常**）。

    返回 {'state': dict|None, 'unreadable': bool}
      state=None 且 unreadable=False => 文件不存在(无闸门)
      unreadable=True               => 文件在但读不出 => 调用方必须 fail-closed
    """
    fp = state_path(path)
    if not os.path.isfile(fp):
        return {"state": None, "unreadable": False}
    try:
        with open(fp, encoding="utf-8-sig") as f:
            j = json.load(f)
        if not isinstance(j, dict):
            return {"state": None, "unreadable": True}
        return {"state": j.get("GLOBAL") or {}, "unreadable": False}
    except Exception:  # noqa: BLE001
        return {"state": None, "unreadable": True}


def _write_state_file(path: str | None, doc: dict) -> None:
    fp = state_path(path)
    d = os.path.dirname(fp)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, fp)


def record(layer: str, action: str, actor: str, reason: str,
           ledger: str | None = None, now=None) -> dict:
    """向审计账本追加一条（append-only，不重写）。失败不抛（留痕不能拖垮交易）。"""
    entry = {"ts": (now or datetime.now()).strftime(_TS_FMT), "layer": layer,
             "action": action, "actor": actor or "?", "reason": reason or ""}
    try:
        fp = ledger_path(ledger)
        # [路线图 #6] 走哈希链: 每条带 prev/hash, 改行/删行/换序/截尾均可验证。
        # 兼容既有记录: 无 hash 的历史条目不追溯补算, 验证器如实报 pre_chain。
        import audit_chain as _AC
        _AC.append(fp, entry, now=now)
    except Exception:  # noqa: BLE001
        # 兜底: 链模块不可用时退回裸追加(留痕优先于格式完美)
        try:
            with open(ledger_path(ledger), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
    return entry


def engage(reason: str, actor: str = "human", path: str | None = None,
           ledger: str | None = None, now=None) -> dict:
    """拉下 GLOBAL 总闸（持久 + 留痕）。"""
    now = now or datetime.now()
    doc = {}
    fp = state_path(path)
    if os.path.isfile(fp):
        try:
            with open(fp, encoding="utf-8-sig") as f:
                doc = json.load(f) or {}
        except Exception:  # noqa: BLE001
            doc = {}          # 读不出也允许覆盖: 人工显式动作优先, 且下面会留痕
    doc["GLOBAL"] = {"engaged": True, "reason": reason, "actor": actor,
                     "since": now.strftime(_TS_FMT)}
    _write_state_file(path, doc)
    record("GLOBAL", "engage", actor, reason, ledger=ledger, now=now)
    return doc["GLOBAL"]


def release(actor: str = "human", reason: str = "", path: str | None = None,
            ledger: str | None = None, now=None) -> dict:
    """解除 GLOBAL 总闸（持久 + 留痕）。"""
    now = now or datetime.now()
    doc = {}
    fp = state_path(path)
    if os.path.isfile(fp):
        try:
            with open(fp, encoding="utf-8-sig") as f:
                doc = json.load(f) or {}
        except Exception:  # noqa: BLE001
            doc = {}
    doc["GLOBAL"] = {"engaged": False, "reason": reason, "actor": actor,
                     "since": now.strftime(_TS_FMT)}
    _write_state_file(path, doc)
    record("GLOBAL", "release", actor, reason, ledger=ledger, now=now)
    return doc["GLOBAL"]


def verdict(account: dict | None = None, strategy: dict | None = None,
            path: str | None = None) -> dict:
    """生产入口: 读 GLOBAL 状态 + 合并三层 => 单一裁决。"""
    g = read_global(path)
    return evaluate(g["state"], account=account, strategy=strategy,
                    global_unreadable=g["unreadable"])


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="三层 Kill Switch: GLOBAL/ACCOUNT/STRATEGY")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="打印当前裁决(默认)")
    g.add_argument("--engage", metavar="原因", help="拉下 GLOBAL 总闸(停止新开仓)")
    g.add_argument("--release", action="store_true", help="解除 GLOBAL 总闸")
    g.add_argument("--ledger", type=int, metavar="N", help="显示最近 N 条审计记录")
    ap.add_argument("--actor", default="human", help="操作者标记(留痕用)")
    args = ap.parse_args(argv)

    if args.engage:
        e = engage(args.engage, actor=args.actor)
        print(f"GLOBAL 已拉闸: {e['reason']} (actor={e['actor']} @{e['since']})")
        return 0
    if args.release:
        e = release(actor=args.actor, reason="人工解除")
        print(f"GLOBAL 已解除 (actor={e['actor']} @{e['since']})")
        return 0
    if args.ledger:
        fp = ledger_path()
        if not os.path.isfile(fp):
            print("(无审计记录)")
            return 0
        for ln in open(fp, encoding="utf-8").read().splitlines()[-args.ledger:]:
            print(ln)
        return 0

    v = verdict()
    print(json.dumps(v, ensure_ascii=False, indent=2))
    return 2 if v["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(_main())
