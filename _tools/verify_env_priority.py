# -*- coding: utf-8 -*-
"""一次性验收: 两个 .env 的**加载顺序与优先级**是否符合设计.

## 设计

  ① 进程环境变量(最高, 永不被 .env 覆盖)
  ② <工作区>/research_trader/.env   (权威, 含密钥)
  ③ <本仓>/A_stock_rotation/.env    (项目级补充)

## 为什么必须实测

旧 `_load_dotenv` 读到**第一个存在的文件就 return** ⇒ 只要 ② 存在, ③
**永远不被读**。后果是"把配置写进本仓 .env"会**静默失效**: 文件在、格式对、
就是不生效 —— 这类失效不会报错, 只会让人以为改过了。

本脚本在**隔离的 os.environ** 下断言三件事:
  1. ② 与 ③ 都被读到(③ 独有的键也能进来);
  2. 同名键冲突时 **② 胜**;
  3. 进程环境变量**不被** .env 覆盖。

**只读**, 不写任何状态。
"""

from __future__ import annotations

import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

_PROBE_KEYS = ("OPENAI_BASE_URL", "OPENAI_MODEL", "LLM_API_TYPE", "LLM_THINK",
               "OLLAMA_NUM_PREDICT", "OPENAI_TIMEOUT_SECONDS",
               "ENV_LOCAL_LOADED")


def _clean():
    for k in _PROBE_KEYS:
        os.environ.pop(k, None)


def main() -> int:
    import llm_commentary as lc

    ws = os.path.join(os.path.dirname(_BASE), "research_trader", ".env")
    local = os.path.join(_BASE, ".env")
    print("=" * 78)
    print("候选文件")
    print("=" * 78)
    for p in lc._DEFAULT_ENV_PATHS:
        print("  %-6s %s" % ("存在" if (p and os.path.exists(p)) else "缺失", p or "(空)"))
    print()
    print("  期望: %s (权威) 与 %s (本地) **都在候选里**" % (ws, local))

    ok = True

    # ---- ① 干净环境: 两文件都应被读到 ----
    _clean()
    lc._load_dotenv()
    print()
    print("=" * 78)
    print("① 干净环境下加载结果")
    print("=" * 78)
    got = {k: os.environ.get(k) for k in _PROBE_KEYS}
    for k, v in got.items():
        print("  %-24s = %s" % (k, v if v is not None else "(未设置)"))
    # 来自 ② 的键
    if got.get("OPENAI_BASE_URL") != "http://192.168.1.5:11434":
        print("  [FAIL] OPENAI_BASE_URL 未被 ② 正确加载")
        ok = False
    # 来自 ③ 的键(③ 独有的键也必须进来, 这正是不 return 才能做到的)
    if got.get("LLM_API_TYPE") != "ollama":
        print("  [FAIL] LLM_API_TYPE 未被 ③ 加载 —— 说明 ③ 仍未被读取")
        ok = False
    if got.get("LLM_THINK") != "0":
        print("  [FAIL] LLM_THINK 未被 ③ 加载")
        ok = False
    # **决定性**: 该键只存在于本仓 .env, 只有真的读了 ③ 才会出现。
    # 用 LLM_API_TYPE 之类两处都有的键证明不了这一点(它可能来自 ②)。
    if got.get("ENV_LOCAL_LOADED") != "local":
        print("  [FAIL] ENV_LOCAL_LOADED 缺失 —— 本仓 .env **根本没被读**")
        ok = False
    else:
        print("  [OK] ENV_LOCAL_LOADED=local —— 本仓 .env 确实被读到(该键仅存在于其中)")
    if ok:
        print("  [OK] ② 与 ③ 均被读到")

    # ---- ② 冲突时 ② 胜 ----
    print()
    print("=" * 78)
    print("② 同名键冲突: research_trader/.env 应胜出")
    print("=" * 78)
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        f1 = Path(td) / "high.env"
        f2 = Path(td) / "low.env"
        f1.write_text("CONFLICT_KEY=from_high\nONLY_HIGH=1\n", encoding="utf-8")
        f2.write_text("CONFLICT_KEY=from_low\nONLY_LOW=1\n", encoding="utf-8")
        _clean()
        os.environ.pop("CONFLICT_KEY", None)
        os.environ.pop("ONLY_HIGH", None)
        os.environ.pop("ONLY_LOW", None)
        orig = lc._DEFAULT_ENV_PATHS
        try:
            lc._DEFAULT_ENV_PATHS = [str(f1), str(f2)]
            lc._load_dotenv()
        finally:
            lc._DEFAULT_ENV_PATHS = orig
        ck = os.environ.get("CONFLICT_KEY")
        print("  CONFLICT_KEY = %s   (期望 from_high)" % ck)
        print("  ONLY_HIGH    = %s" % os.environ.get("ONLY_HIGH"))
        print("  ONLY_LOW     = %s   (靠后的文件也必须被读到)" % os.environ.get("ONLY_LOW"))
        if ck != "from_high" or os.environ.get("ONLY_LOW") != "1":
            print("  [FAIL] 优先级或「读全部文件」行为不符")
            ok = False
        else:
            print("  [OK] 先读者优先, 且未提前 return")

    # ---- ③ 进程环境变量不被覆盖 ----
    print()
    print("=" * 78)
    print("③ 进程环境变量优先级最高(不被 .env 覆盖)")
    print("=" * 78)
    _clean()
    os.environ["OPENAI_MODEL"] = "set-by-process"
    lc._load_dotenv()
    print("  OPENAI_MODEL = %s   (期望 set-by-process)" % os.environ.get("OPENAI_MODEL"))
    if os.environ.get("OPENAI_MODEL") != "set-by-process":
        print("  [FAIL] 进程环境变量被 .env 覆盖了")
        ok = False
    else:
        print("  [OK] 未被覆盖")

    print()
    print("=" * 78)
    print("结论: %s" % ("全部通过" if ok else "**存在失败项**"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
