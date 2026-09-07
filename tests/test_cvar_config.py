# -*- coding: utf-8 -*-
"""快速验证 CVaR-PPO 参数配置集成."""
from __future__ import annotations

import os
import sys
import inspect
import subprocess

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)


def test_env_override():
    os.environ["CVAR_ALPHA"] = "0.03"
    os.environ["CVAR_COEF"] = "0.2"
    # reload config to pick up env vars
    import importlib
    import config as cfg_mod
    importlib.reload(cfg_mod)
    from config import CVAR_PPO
    assert CVAR_PPO["cvar_alpha"] == 0.03, f"Expected 0.03, got {CVAR_PPO['cvar_alpha']}"
    assert CVAR_PPO["cvar_coef"] == 0.2, f"Expected 0.2, got {CVAR_PPO['cvar_coef']}"
    print("1. 环境变量覆盖: OK")


def test_default_config():
    # clean env and reload
    keys = ["CVAR_ALPHA", "CVAR_COEF"]
    for k in keys:
        os.environ.pop(k, None)
    import importlib
    import config as cfg_mod
    importlib.reload(cfg_mod)
    from config import CVAR_PPO
    assert CVAR_PPO["cvar_alpha"] == 0.05, f"Expected 0.05, got {CVAR_PPO['cvar_alpha']}"
    assert CVAR_PPO["cvar_coef"] == 0.1, f"Expected 0.1, got {CVAR_PPO['cvar_coef']}"
    print("2. config.py 默认值: OK")


def test_cli_help():
    result = subprocess.run(
        [sys.executable, "drl_train.py", "--help"],
        capture_output=True, text=True, cwd=_BASE,
    )
    assert "--cvar_alpha" in result.stdout, "CLI --cvar_alpha 未在 help 中"
    assert "--cvar_coef" in result.stdout, "CLI --cvar_coef 未在 help 中"
    print("3. CLI --cvar_alpha / --cvar_coef: OK")


def test_run_drl_train_signature():
    from drl_train import run_drl_train
    sig = inspect.signature(run_drl_train)
    params = list(sig.parameters.keys())
    assert "cvar_alpha" in params, f"run_drl_train 缺少 cvar_alpha 参数: {params}"
    assert "cvar_coef" in params, f"run_drl_train 缺少 cvar_coef 参数: {params}"
    print("4. run_drl_train 参数签名: OK")


def test_cvar_ppo_init_signature():
    from drl_train import CVaR_PPO
    sig = inspect.signature(CVaR_PPO.__init__)
    params = list(sig.parameters.keys())
    assert "cvar_alpha" in params, f"CVaR_PPO.__init__ 缺少 cvar_alpha: {params}"
    assert "cvar_coef" in params, f"CVaR_PPO.__init__ 缺少 cvar_coef: {params}"
    print("5. CVaR_PPO.__init__ 参数签名: OK")


if __name__ == "__main__":
    test_env_override()
    test_default_config()
    test_cli_help()
    test_run_drl_train_signature()
    test_cvar_ppo_init_signature()
    print("\n所有 5 个配置集成测试通过!")