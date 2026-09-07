# -*- coding: utf-8 -*-
"""py3.14 因子挖掘工具集入口 — ai-factor-lab / QuantGplearn / FactorMiner.

用法:
    python run_fm314.py quick-check                   # ai-factor-lab: 默认验证融合因子
    python run_fm314.py quick-check [-e expr] [-n name]  # ai-factor-lab: 自定义因子
    python run_fm314.py gp-mine [--days 60] [--pop 200] [--gen 10]  # QuantGplearn: GP挖掘
    python run_fm314.py gp-smoke                      # QuantGplearn: 冒烟测试
    python run_fm314.py gp-list                       # QuantGplearn: 列出特征
    python run_fm314.py miner-register <name> <expr>  # FactorMiner: 登记候选因子
    python run_fm314.py miner-report                  # FactorMiner: 查看台账
    python run_fm314.py registry                      # FactorMiner: 查看 registry

py3.14 venv 路径: .venv314/
"""
import os
import sys
import subprocess
import json

_BASE = os.path.dirname(os.path.abspath(__file__))
_VENV = os.path.join(_BASE, ".venv314", "Scripts", "python.exe")
_FM = os.path.join(_BASE, "factor_mine")


def _py314(args: list[str], **kwargs) -> int:
    print(f"[py3.14] {' '.join(args)}", flush=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = _BASE + os.pathsep + _FM + os.pathsep + env.get("PYTHONPATH", "")
    cp = subprocess.run([_VENV, *args], capture_output=False, env=env, **kwargs)
    return cp.returncode


def main():
    if not os.path.exists(_VENV):
        print(f"ERROR: .venv314 不存在, 请先运行:\n"
              f"  py -3.14 -m venv {os.path.join(_BASE, '.venv314')}")
        sys.exit(1)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    if cmd == "quick-check":
        # ai-factor-lab: factor_quick_check.py
        script = os.path.join(_FM, "factor_quick_check.py")
        sys.exit(_py314([script, *rest]))

    elif cmd == "gp-mine":
        # QuantGplearn: gp_mine_daily.py
        script = os.path.join(_FM, "gp_mine_daily.py")
        sys.exit(_py314([script, "run", *rest]))

    elif cmd == "gp-smoke":
        script = os.path.join(_FM, "gp_mine_daily.py")
        sys.exit(_py314([script, "smoke", *rest]))

    elif cmd == "gp-list":
        script = os.path.join(_FM, "gp_mine_daily.py")
        sys.exit(_py314([script, "list", *rest]))

    elif cmd == "miner-register":
        # FactorMiner: 登记候选
        name = rest[0] if len(rest) > 0 else input("因子名: ")
        expr = rest[1] if len(rest) > 1 else input("表达式: ")
        code = f"""
import sys; sys.path.insert(0, {_FM!r})
from factor_mine import ledger
cand = ledger.register({name!r}, {expr!r}, 'manual', 'py3.14 FactorMiner')
print('已登记:', cand)
"""
        sys.exit(_py314(["-c", code]))

    elif cmd == "miner-report":
        code = f"""
import sys; sys.path.insert(0, {_FM!r})
from factor_mine import ledger
rows = ledger.load()
print(f"台账共 {{len(rows)}} 条记录:")
for r in rows:
    print(f"  {{r.get('name','?'):20s}} expr={{r.get('expr',''):30s}} "
          f"source={{r.get('source','')}}  ts={{r.get('created_at','')}}")
"""
        sys.exit(_py314(["-c", code]))

    elif cmd == "registry":
        reg = os.path.join(_BASE, "data", "factor_mine", "factor_registry.json")
        if not os.path.exists(reg):
            print("factor_registry.json 不存在")
            sys.exit(1)
        with open(reg) as f:
            d = json.load(f)
        print(f"FactorRegistry v{d.get('version')}  (更新: {d.get('updated_at')})")
        print(f"  Library: {len(d.get('library',{}).get('factors',{}))} 个因子")
        for k, v in (d.get("library", {}).get("factors", {})).items():
            print(f"    {k:20s} [{v.get('category','')}] {v.get('desc','')[:40]}")
        print(f"  Fusion: {len(d.get('fusion',{}).get('factors',{}))} 个因子")
        for k, v in (d.get("fusion", {}).get("factors", {})).items():
            print(f"    {k:20s} w={v.get('weight','')}  role={v.get('role','')}")

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()