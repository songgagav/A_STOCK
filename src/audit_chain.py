# -*- coding: utf-8 -*-
"""哈希链审计（路线图 #6）：让 append-only 账本**可验证未被篡改**。

为什么需要它
------------
本仓已有两个 append-only 账本（`data/kill_switch_ledger.jsonl` 闸门事件、
`data/order_audit.jsonl` 订单流水），约定是"不删除、不重写"。但**约定不是证据**:
一个 JSONL 文件被手工改掉一行、删掉一行、或调换两行, 从外表完全看不出来 ——
而这两个账本恰恰是事后追责（谁拉的闸、哪笔单被放行）的唯一依据。

本模块给每条记录加上 `prev`(前一条的哈希) + `hash`(本条内容的哈希), 于是:
  · 改一行 => 该行 hash 不符
  · 删一行 => 下一行的 prev 链接断裂
  · 调换两行 => 链接断裂
  · 截断尾部 => 通过**链头快照**(单独文件记录条数与末哈希)发现

**不追溯改写历史**（重要取舍）
------------------------------
已存在的记录没有哈希字段。选择**不为它们补算**: 补算等于事后制造可信度 —— 那些记录在写下的
那一刻并没有被承诺不可改, 事后补上的哈希证明不了它们的原始性。故链从**下一条**记录开始,
验证器把之前的条数如实报为 `pre_chain`, 让人一眼看出"哪一段是有链的、哪一段不是"。

**能力边界（不夸大）**
----------------------
纯本地链只能防"改了忘同步改后面"这类篡改。**真正的对手若能同时改账本与链头文件,
本地链挡不住** —— 那需要把链头哈希外发（另一台机器/公开渠道）做锚定, 属该场景的
下一步而非本模块的能力。此处如实标注, 不宣称"不可篡改"。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

GENESIS = "0" * 64
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def head_path(ledger: str, path: str | None = None) -> str:
    """链头快照路径（记录条数与末哈希, 用于发现尾部截断）。"""
    return path or (ledger + ".head.json")


def _canon(rec: dict) -> str:
    """规范化序列化: 键排序 + 紧凑分隔符 => 同一条记录在任何机器上哈希一致。"""
    body = {k: v for k, v in rec.items() if k != "hash"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_hash(rec: dict) -> str:
    return hashlib.sha256(_canon(rec).encode("utf-8")).hexdigest()


def _read_lines(path: str) -> list:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def _last_link(path: str) -> tuple:
    """返回 (条数, 末条哈希)；末条无 hash（历史记录）则哈希为 GENESIS。"""
    lines = _read_lines(path)
    if not lines:
        return 0, GENESIS
    try:
        last = json.loads(lines[-1])
        return len(lines), (last.get("hash") or GENESIS)
    except Exception:  # noqa: BLE001
        return len(lines), GENESIS


def _lock(fh):
    """尽力加独占锁（Windows: msvcrt；POSIX: fcntl）。失败不阻断 —— 留痕优先于完美串行。"""
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return True
    except Exception:  # noqa: BLE001
        return False


def _unlock(fh):
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001
        pass


def append(ledger: str, record: dict, now=None, write_head: bool = True) -> dict:
    """追加一条带链的记录。**失败不抛** —— 留痕路径不得拖垮交易。

    返回写入的记录（含 prev/hash）；失败时返回原记录。

    锁加在**独立的 .lock 文件**上, 不加在账本自身: Windows 的 msvcrt.locking 是
    **强制锁**(POSIX 是劝告锁), 锁住账本尾部字节会让随后重开句柄读取账本的代码
    (正是本函数的 `_last_link`) 读失败 —— 表现是"每条记录都以为自己是链首, prev 全是
    GENESIS", 而异常被宽 except 吞掉, 链静默退化成没有链。实测踩到过。
    """
    rec = dict(record or {})
    lockf = None
    locked = False
    try:
        d = os.path.dirname(ledger)
        if d:
            os.makedirs(d, exist_ok=True)
        # 独立锁文件: 读账本 -> 写新行 必须在临界区内完成, 否则并发写会分叉出两条同 prev 的记录
        lockf = open(ledger + ".lock", "a+")
        locked = _lock(lockf)
        n, prev = _last_link(ledger)
        rec["seq"] = n                      # 序号: 便于人读与定位
        rec["prev"] = prev
        rec["hash"] = compute_hash(rec)
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if write_head:
            try:
                with open(head_path(ledger), "w", encoding="utf-8") as hf:
                    json.dump({"n": rec["seq"] + 1, "last_hash": rec["hash"],
                               "ts": (now or datetime.now()).strftime(_TS_FMT)},
                              hf, ensure_ascii=False, indent=2)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            if locked and lockf is not None:
                _unlock(lockf)
            if lockf is not None:
                lockf.close()
        except Exception:  # noqa: BLE001
            pass
    return rec


def verify(ledger: str, check_head: bool = True) -> dict:
    """验证账本链。返回 {ok, n, pre_chain, broken_at, reason, head_ok}。"""
    lines = _read_lines(ledger)
    out = {"ledger": ledger, "ok": True, "n": len(lines), "pre_chain": 0,
           "broken_at": None, "reason": None, "head_ok": None}
    prev = GENESIS
    started = False
    for i, ln in enumerate(lines, start=1):
        try:
            rec = json.loads(ln)
        except Exception as e:  # noqa: BLE001
            out.update(ok=False, broken_at=i, reason=f"第 {i} 行不是合法 JSON: {e}")
            return out
        if "hash" not in rec:
            # 链开始前的历史记录: 如实计数, 不当作损坏（也不追溯补算）
            if not started:
                out["pre_chain"] += 1
                continue
            out.update(ok=False, broken_at=i, reason=f"第 {i} 行缺 hash（链中途断裂）")
            return out
        started = True
        if rec.get("prev") != prev:
            out.update(ok=False, broken_at=i,
                       reason=(f"第 {i} 行 prev 链接断裂（期望 {prev[:12]}…, "
                               f"实际 {str(rec.get('prev'))[:12]}…）=> 该行之前被删/被换序"))
            return out
        if compute_hash(rec) != rec.get("hash"):
            out.update(ok=False, broken_at=i,
                       reason=f"第 {i} 行内容与自身 hash 不符 => 该行被改过")
            return out
        prev = rec["hash"]
    if check_head:
        hp = head_path(ledger)
        if os.path.isfile(hp):
            try:
                head = json.load(open(hp, encoding="utf-8"))
                if int(head.get("n") or -1) != len(lines):
                    out.update(ok=False, head_ok=False,
                               reason=(f"链头条数 {head.get('n')} != 实际 {len(lines)} "
                                       f"=> 尾部被截断或追加未记录"))
                elif head.get("last_hash") not in (None, prev):
                    out.update(ok=False, head_ok=False,
                               reason="链头末哈希与账本末行不符 => 尾部被改/被截断")
                else:
                    out["head_ok"] = True
            except Exception as e:  # noqa: BLE001
                out["head_ok"] = None
                out["head_reason"] = f"链头不可读: {e}"
    return out


DEFAULT_LEDGERS = ("data/kill_switch_ledger.jsonl", "data/order_audit.jsonl",
                   "data/drl_degrade_events.jsonl")


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="哈希链审计: 验证 append-only 账本未被篡改")
    ap.add_argument("--ledger", action="append", help="要验证的账本路径（可重复）")
    ap.add_argument("--all", action="store_true", help="验证本仓已知账本")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    targets = args.ledger or []
    if args.all or not targets:
        targets = [os.path.join(_repo_root(), p) for p in DEFAULT_LEDGERS]
    results = [verify(t) for t in targets]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            rel = os.path.relpath(r["ledger"], _repo_root())
            tag = "OK  " if r["ok"] else "FAIL"
            extra = f" (链前 {r['pre_chain']} 条未覆盖)" if r["pre_chain"] else ""
            print(f"[{tag}] {rel}: {r['n']} 条{extra}")
            if not r["ok"]:
                print(f"       {r['reason']}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
