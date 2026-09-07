# ============================================================
# ml_fusion_bridge.py -- 三模型融合(f_ml 因子)接入轮动选股
#
# 改造: 把 ml_fusion(独立 venv) 的 XGBoost+LightGBM+CatBoost Stacking
#   融合预测作为新因子 f_ml 接入 selector 打分池.
#   按项目纪律: 融合预测先做"新因子"独立监控, 积累足够实盘数据后再注入
#   DRL 观测(本模块不触碰 DRL).
#
# 能力:
#   compute_fml(canons, as_of)  调独立 venv 的 fusion.predict_daily.py
#     对 as_of 日期现算特征并输出未来5日收益回归打分
#     返回 {available, as_of, scores:{canon:float}, meta}.
#
# 设计约束 (与 graphrag_bridge 一致):
#   - 任意失败(脚本缺失/超时/非JSON/rc!=0/空输入)一律容错返回
#     {"available": False}, 绝不抛异常, 保证 selector 主链路不受影响.
#   - 通过 subprocess argv 列表传参, 规避 shell 对前导0代码(如 000001)
#     的数字强转丢失;(比命令行字符串更安全).
#   - canon('600519.SH') 转 6 位代码('600519')再喂给 DuckDB 侧.
#   - 运行一次约 0.4~5s(取决于标的数), 用超时保护.
# ============================================================

from __future__ import annotations

import json
import logging
import os
import subprocess

_LOG = logging.getLogger("ml_fusion_bridge")

# 融合环境根 (含独立 venv 与预测脚本/模型)
_FUSION_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "ml_fusion")
)
FUSION_VENV_PY = os.path.join(_FUSION_ROOT, ".venv", "Scripts", "python.exe")
# 默认模型: 排名学习主信号 (2026-09-04 接入). 样本外 2019-2021 验证 RankIC
# 0.079 vs 原 fwd5 回归 0.044 (+81%), 预测截面排名分位比分收益稳健得多.
# 回退: 若 rank 模型缺失则用 fwd5 回归.
FML_MODEL = os.path.join(_FUSION_ROOT, "data", "models",
                         "stacking_rank5_lambda.pkl")
FML_FALLBACK_MODEL = os.path.join(_FUSION_ROOT, "data", "models",
                                  "stacking_fwd5_reg.pkl")

# 环境变量可覆盖 (便于不同机器/模型切换)
_FUSION_ROOT = os.environ.get("ML_FUSION_ROOT", _FUSION_ROOT)
FUSION_VENV_PY = os.environ.get("ML_FUSION_VENV_PY",
                                os.path.join(_FUSION_ROOT, ".venv", "Scripts", "python.exe"))
FML_MODEL = os.environ.get("ML_FUSION_RANK_MODEL", FML_MODEL)
FML_FALLBACK_MODEL = os.environ.get("ML_FUSION_FWD5_MODEL", FML_FALLBACK_MODEL)

_TIMEOUT_S = float(os.environ.get("ML_FUSION_TIMEOUT_S", "60") or 60)

# 打分融合权重: 融合因子在 selector 综合分中的占比(当前固定, 后续可由
# weight_optimizer 收入 weights.json 自适应). 小权重起步, 稳定后再上调.
FML_WEIGHT = float(os.environ.get("ML_FUSION_WEIGHT", "0.10") or 0.10)

# [2026-09-05] 因子信号方向开关. 曾因近3日1日IC为负而启反转测试;
# 连续5交易日检测(见 ml_fusion/data/factor_daily_check_rev.json)显示反转后
# IC均值=-0.009/ICIR=-0.068/高组未跑赢 => 反转不整体成立, 默认回退为不反转.
# 如需手工启用: FML_INVERT_SIGN=1. (rank5 模型按5日fwd5训练, 反转令样本内IC变负)
FML_INVERT_SIGN = os.environ.get("FML_INVERT_SIGN", "0") == "1"


def _canon_to_code(canon: str) -> str:
    """'600519.SH' -> '600519'; 已是纯6位则原样返回."""
    return str(canon).split(".")[0]


def compute_fml(canons: list[str], as_of: str,
                model_path: str | None = None) -> dict:
    """对 as_of 日的候选池 canons 生成融合打分 f_ml.

    默认用排名学习主信号 stacking_rank5_lambda (预测截面排名分位, 更稳健);
    缺失时回退 fwd5 收益回归模型. 亦可显式传入 model_path 覆盖.
    Returns
    -------
    dict: {
      "available": bool,
      "as_of": str,
      "scores": {canon: float},   # 排名/收益预测(相对强弱打分, 越大越强)
      "n": int,
      "meta": {...},              # elapsed_s / error / scanned / model
    }
    失败时 available=False, 绝不抛异常.
    """
    if not canons:
        return {"available": False, "as_of": as_of, "scores": {}, "n": 0,
                "meta": {"error": "empty canons"}}
    if not os.path.exists(FUSION_VENV_PY):
        return {"available": False, "as_of": as_of, "scores": {}, "n": 0,
                "meta": {"error": "fusion venv 缺失"}}
    # 模型选择: 显式 > rank5(默认) > fwd5(回退)
    chosen = model_path or FML_MODEL
    if not os.path.exists(chosen) and os.path.exists(FML_FALLBACK_MODEL):
        chosen = FML_FALLBACK_MODEL
    if not os.path.exists(chosen):
        return {"available": False, "as_of": as_of, "scores": {}, "n": 0,
                "meta": {"error": f"融合模型缺失: {chosen}"}}

    codes = [_canon_to_code(c) for c in canons]
    cmd = [FUSION_VENV_PY, "-m", "fusion.predict_daily",
           "--date", as_of, "--symbols", ",".join(codes),
           "--model", chosen]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            timeout=_TIMEOUT_S, cwd=_FUSION_ROOT)
    except subprocess.TimeoutExpired as e:
        _LOG.warning("f_ml 预测超时(%.0fs) n=%s: %s", _TIMEOUT_S, len(canons), e)
        return {"available": False, "as_of": as_of, "scores": {}, "n": 0,
                "meta": {"error": f"TimeoutExpired: {e}"}}
    except Exception as e:  # noqa: BLE001
        _LOG.warning("f_ml 预测启动失败: %s", e)
        return {"available": False, "as_of": as_of, "scores": {}, "n": 0,
                "meta": {"error": f"{type(e).__name__}: {e}"}}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-500:]
        _LOG.warning("f_ml 预测失败 rc=%s n=%s: %s", proc.returncode,
                     len(canons), err[:200])
        return {"available": False, "as_of": as_of, "scores": {}, "n": 0,
                "meta": {"error": f"rc={proc.returncode}: {err[:200]}"}}

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        _LOG.warning("f_ml 预测输出非 JSON: %s", e)
        return {"available": False, "as_of": as_of, "scores": {}, "n": 0,
                "meta": {"error": f"JSONDecodeError: {e}"}}

    if not data.get("ok"):
        meta = data.get("meta") or {}
        return {"available": False, "as_of": as_of, "scores": {}, "n": 0,
                "meta": meta}

    raw = data.get("scores") or {}
    # 6位 code -> canon 回映射; 只保留能正确回映(且 isfinite 已由预测侧过滤)
    by_code = {c: c for c in codes}
    scores: dict[str, float] = {}
    for canon in canons:
        code = _canon_to_code(canon)
        if code in raw and isinstance(raw[code], (int, float)):
            scores[canon] = float(raw[code])
    if FML_INVERT_SIGN and scores:
        # factor_signal = -1 * factor_signal  # 反转信号方向
        scores = {c: -1.0 * v for c, v in scores.items()}
    _meta = dict(data.get("meta") or {})
    _meta["model"] = os.path.basename(chosen)
    return {
        "available": bool(scores),
        "as_of": as_of,
        "scores": scores,
        "n": len(scores),
        "meta": _meta,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="f_ml 融合因子桥接 (调试用)")
    ap.add_argument("--date", required=True)
    ap.add_argument("--symbols", required=True, help="canon, 逗号分隔; 如 600519.SH,000001.SZ")
    a = ap.parse_args()
    canons = [s.strip() for s in a.symbols.split(",") if s.strip()]
    r = compute_fml(canons, a.date)
    print(json.dumps(r, ensure_ascii=False, default=str))