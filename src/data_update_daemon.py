# -*- coding: utf-8 -*-
"""数据更新守护 (data_update_daemon).

按数据类型就绪窗口, 在交易日盘后自动补齐缺口 (与 daemon/gate_refresh_daemon
并行常驻). 每次执行都先做"滞后巡检", 仅对滞后的数据执行补拉, 天然幂等
(重复触发/重复日期由目标脚本或 h5i 单调追加跳过).

窗口 (参考数据商典型更新时刻, 均保守延后避免源未就绪):
  15:45  日线主链同步; 双缺口时才允许 Baostock 回补
  16:40  当日估值快照 (东财; 东财不可达时失败并记录, 下轮/次日重试)
  16:45  北向资金 (northbound_sync, 若滞后)
  17:20  两融 (margin_sync, 若滞后且通道可用)
  19:10  收盘主流程由主 daemon 触发 (见 daemon.py, 已后移至 19:10)

用法:  python src/data_update_daemon.py             # 常驻
       python src/data_update_daemon.py --once      # 立即执行一轮
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, date, timedelta

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(_BASE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
os.chdir(_BASE)

from daily_data_completion import (  # noqa: E402
    plan_daily_completion,
    run_daily_completion,
)

PY = sys.executable
MARK = os.path.join(_BASE, "data", "data_update_last.json")
INTERVAL_S = int(os.environ.get("DATA_UPDATE_INTERVAL_S", "300"))
# 各阶段开始分钟 (当日 HH:MM 换算)
T_DAILY = 15 * 60 + 45
T_VALU = 16 * 60 + 40
T_NB = 16 * 60 + 45
T_MF = 16 * 60 + 50          # 板块资金流快照
T_MARGIN = 17 * 60 + 20      # 两融 (SSE 当日可见; 深/京 T+1 由次日运行补齐)
T_CLOSE_HINT = 19 * 60 + 10
# 阶段失败后的重试节流(秒)
FAIL_THROTTLE_S = int(os.environ.get("DATA_UPDATE_FAIL_THROTTLE_S", "1800"))


def _log(msg: str) -> None:
    print(f"[data_update] {datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _now_min() -> int:
    n = datetime.now()
    return n.hour * 60 + n.minute


def _trade_days_range(lo: date, hi: date) -> list[date]:
    try:
        import factor_fusion as ff
        df = ff._sql(f"SELECT DISTINCT CAST(ts AS DATE) d FROM daily_bars "
                     f"WHERE CAST(ts AS DATE) >= DATE '{lo}' AND CAST(ts AS DATE) <= DATE '{hi}' "
                     f"ORDER BY d")
        return [r.d.date() if hasattr(r.d, "date") else r.d for r in df.itertuples()]
    except Exception:
        return []


def _valu_stale_day() -> str | None:
    """valuation_snapshot 滞后时返回应补的交易日."""
    import factor_fusion as ff
    try:
        mx = ff._sql("SELECT MAX(CAST(ts AS DATE)) d FROM valuation_snapshot").iloc[0, 0]
        base = ff._sql("SELECT MAX(CAST(ts AS DATE)) d FROM daily_bars").iloc[0, 0]
    except Exception:
        return None
    if base is None or mx is None or mx >= base:
        return None
    days = _trade_days_range(mx + timedelta(days=1), base)
    return str(days[-1]) if days else None


def _state() -> dict:
    try:
        with open(MARK, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(st: dict) -> None:
    os.makedirs(os.path.dirname(MARK), exist_ok=True)
    with open(MARK, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _run_once(stage: str, args: list[str], day: str | None = None) -> dict:
    cmd = [PY, *args]
    if day:
        cmd += ["--day", day]
    try:
        # 子进程是本仓自己的 Python 脚本, 输出含中文(进度/告警/异常)。
        # 不显式给 encoding 时会按 cp936 解 —— 要么解成乱码, 要么在读取线程里
        # 抛 UnicodeDecodeError 而 `subprocess.run` **不抛、静默返回 stdout=None**,
        # 于是 `out` 变成空串, 事后从 stage 记录里**再也看不到子进程说过什么**。
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1500)
        return {"ok": r.returncode == 0, "stage": stage, "day": day,
                "out": (r.stdout or "")[-2000:], "err": (r.stderr or "")[-300:],
                "decode_ok": r.stdout is not None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stage": stage, "day": day, "err": str(e)[:200]}


def _primary_daily_sync() -> dict:
    """运行 StockDB→H5i 主链，并补齐自动判定所需的事实字段。"""
    import engine_bars_sync as _E
    import h5i_sync as _H

    result = _E.sync_to_latest(apply=True)
    freshness = _E.freshness(result.get("engine_last_day"))
    watermark = _H.max_bar_date(force=True)
    result["h5i_max_after"] = str(watermark) if watermark else None
    result["expected_day"] = freshness.get("expected_day")
    result["calendar_source"] = freshness.get("calendar_source")
    result["calendar_strength"] = freshness.get("calendar_strength")
    result["freshness"] = freshness
    return result


def _backfill_interpreter() -> str | None:
    """按真实 import 探测可用 Baostock 解释器，不按路径猜测。"""
    candidates = [PY]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.extend([
        os.path.join(root, ".venv310", "Scripts", "python.exe"),
        os.path.join(root, ".venv310", "bin", "python"),
    ])
    seen = set()
    for exe in candidates:
        if not exe or exe in seen or not os.path.isfile(exe):
            continue
        seen.add(exe)
        try:
            probe = subprocess.run(
                [exe, "-c", "import baostock"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            if probe.returncode == 0:
                return exe
        except Exception:  # noqa: BLE001
            continue
    return None


def _run_backfill_subprocess(missing_days: list[str]) -> dict:
    """在有 Baostock 的解释器中运行已审计回补，并读取 JSON 回执。"""
    days = [str(day) for day in (missing_days or []) if day]
    if not days:
        return {"ok": False, "error": "没有待补交易日", "written": []}
    exe = _backfill_interpreter()
    if not exe:
        return {
            "ok": False,
            "error": "找不到装了 baostock 的解释器",
            "missing_days": days,
            "written": [],
        }

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "data")
    os.makedirs(out_dir, exist_ok=True)
    fd, out_fp = tempfile.mkstemp(prefix="backfill_last_run_", suffix=".json",
                                   dir=out_dir)
    os.close(fd)
    pretty = [f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d for d in days]
    cmd = [exe, os.path.join(root, "src", "baostock_backfill.py"),
           "--days", *pretty, "--write", "--out", out_fp]
    try:
        proc = subprocess.run(
            cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=5400,
        )
        result = {
            "ok": proc.returncode == 0,
            "exit": proc.returncode,
            "interpreter": exe,
            "missing_days": days,
            "out_fp": out_fp,
            "written": [],
        }
        try:
            with open(out_fp, encoding="utf-8-sig") as f:
                full = json.load(f)
            result["written"] = full.get("written") or []
            result["verify"] = full.get("verify")
            result["days"] = full.get("days")
            result["provenance_fp"] = full.get("provenance_fp")
        except Exception as exc:  # noqa: BLE001
            result["read_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        if proc.returncode != 0:
            result["tail"] = (proc.stdout or "")[-400:]
        return result
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "interpreter": exe,
            "missing_days": days,
            "out_fp": out_fp,
            "written": [],
        }


# 收盘后窗口与盘前窗口 (分钟, 用于估值/两融: 源仅在数据商发布后才可及)
_EVENING = (16 * 60, 22 * 60 + 30)
_MORNING = (6 * 60 + 30, 9 * 60)


def _in_publish_windows() -> bool:
    m = _now_min()
    return any(lo <= m <= hi for lo, hi in (_EVENING, _MORNING))


def _due(st: dict, stage: str, force: bool) -> bool:
    """失败节流: 距上次失败 >= FAIL_THROTTLE_S 才允许重试."""
    if force:
        return True
    lf = (st.get("fail") or {}).get(stage)
    if not lf:
        return True
    try:
        last = datetime.fromisoformat(lf)
        return (datetime.now() - last).total_seconds() >= FAIL_THROTTLE_S
    except Exception:
        return True


def _mark_fail(st: dict, stage: str) -> None:
    st.setdefault("fail", {})[stage] = datetime.now().isoformat()


def run_round(force: bool = False) -> dict:
    st = _state()
    today = datetime.now().strftime("%Y-%m-%d")
    t0 = _now_min()
    out = {}

    def after(T: int) -> bool:
        return force or t0 >= T

    # 1) 日线主链优先 + 双缺口时的 Baostock 回补 (15:45 后)
    if after(T_DAILY) and _due(st, "daily", force):
        import backfill_trigger as _BT
        out["daily"] = run_daily_completion(
            primary_runner=_primary_daily_sync,
            fallback_runner=_run_backfill_subprocess,
            enabled=_BT.is_enabled(),
            lookback_days=_BT.lookback_days(),
            today=today,
        )
        if out["daily"].get("ok"):
            st["daily_success_day"] = today
            (st.get("fail") or {}).pop("daily", None)
        else:
            _mark_fail(st, "daily")
    # 2) 估值快照 (16:40 后; 东财全市场接口 2026-09-08 起被反爬断连, 改用腾讯 qt.gtimg 源)
    if after(T_VALU):
        d = _valu_stale_day()
        if d:
            key = f"valuation:{d}"
            if (st.get(key) != today or force) and _due(st, "valuation", force):
                out[key] = _run_once("valuation", ["scripts/tencent_valuation.py"], day=d)
                if out[key]["ok"]:
                    st[key] = today
                    (st.get("fail") or {}).pop("valuation", None)
                else:
                    _mark_fail(st, "valuation")
    # 3) 板块资金流快照 (16:50 后; 同日幂等, 失败节流重试)
    if after(T_MF) and _due(st, "money_flow", force):
        out["money_flow"] = _run_once("money_flow", ["src/money_flow_sync.py", "sync"])
        if out["money_flow"]["ok"]:
            (st.get("fail") or {}).pop("money_flow", None)
        else:
            _mark_fail(st, "money_flow")
    # 4) 两融 (17:20 后且处于发布窗口; margin_sync 自带幂等/水位; 深京 T+1 次日自愈)
    if after(T_MARGIN) and (force or _in_publish_windows()) and _due(st, "margin", force):
        out["margin"] = _run_once("margin", ["src/margin_sync.py", "sync", "--days", "2"])
        _o = out["margin"].get("out") or ""
        # 成功判定: 退出码 0 且无缺腿(缺腿=SSE/深/京当日未发布, 视为未完成以便节流续试)
        incomplete = '"missing_legs": {' not in _o or '"missing_legs": {}' in _o
        if out["margin"]["ok"] and incomplete:
            (st.get("fail") or {}).pop("margin", None)
        else:
            _mark_fail(st, "margin")
    if force or t0 >= T_CLOSE_HINT:
        out["_note"] = "收盘主流程由 daemon.py 在收盘窗口执行 (见 daemon.py)"
    # Persist the complete round receipt so retries and the dashboard can
    # distinguish a primary sync, a fallback backfill, and a blocked attempt.
    # ``default=str`` keeps third-party date-like values auditably serializable
    # without changing the execution result or any production state.
    st["last_round_at"] = datetime.now().isoformat()
    st["last_round"] = json.loads(json.dumps(out, ensure_ascii=False, default=str))
    _save(st)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.once:
        rep = run_round(force=True)
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        return
    _log(f"启动 (interval={INTERVAL_S}s) 窗口: 日线15:45 / 估值16:40 / 收盘由主daemon")
    while True:
        try:
            rep = run_round()
            for k, v in rep.items():
                if isinstance(v, dict) and "ok" in v:
                    tail = (v.get("out") or v.get("err") or "")[:90]
                    _log(f"{k}: ok={v['ok']} ({tail})")
        except Exception as e:  # noqa: BLE001
            _log(f"轮询异常: {e}")
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
