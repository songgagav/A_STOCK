# -*- coding: utf-8 -*-
"""数据更新守护 (data_update_daemon).

按数据类型就绪窗口, 在交易日盘后自动补齐缺口 (与 daemon/gate_refresh_daemon
并行常驻). 每次执行都先做"滞后巡检", 仅对滞后的数据执行补拉, 天然幂等
(重复触发/重复日期由目标脚本或 h5i 单调追加跳过).

窗口 (参考数据商典型更新时刻, 均保守延后避免源未就绪):
  14:50  跟踪标的五档盘口旁路快照 (仅在交易时段, 不可盘后伪造)
  15:45  日线主链同步; 双缺口时才允许 Baostock 回补
  16:40  当日估值快照 (东财; 东财不可达时失败并记录, 下轮/次日重试)
  16:45  北向资金 (northbound_sync, 若滞后)
  17:20  两融 (margin_sync, 若滞后且通道可用)
  18:00  龙虎榜/大宗交易/新闻独立研究证据 (非策略输入)
  19:10  收盘主流程由主 daemon 触发 (见 daemon.py, 已后移至 19:10)

用法:  python src/data_update_daemon.py             # 常驻
       python src/data_update_daemon.py --once      # 立即执行一轮
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
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
T_ORDERBOOK = 14 * 60 + 50
T_ORDERBOOK_END = 15 * 60
T_DAILY = 15 * 60 + 45
T_VALU = 16 * 60 + 40
T_NB = 16 * 60 + 45
T_MF = 16 * 60 + 50          # 板块资金流快照
T_MARGIN = 17 * 60 + 20      # 两融 (SSE 当日可见; 深/京 T+1 由次日运行补齐)
T_AUX_RESEARCH = 18 * 60     # 研究用旁路数据; 不影响任何生产决策
T_CLOSE_HINT = 19 * 60 + 10
# 阶段失败后的重试节流(秒)
FAIL_THROTTLE_S = int(os.environ.get("DATA_UPDATE_FAIL_THROTTLE_S", "1800"))
# Match run_daily's 90-minute ceiling: a full 5,000+ symbol fetch at the
# adapter's 200ms request interval alone takes more than 15 minutes.
BACKFILL_TIMEOUT_S = int(os.environ.get("DATA_UPDATE_BACKFILL_TIMEOUT_S", "5400"))
AUXILIARY_TIMEOUT_S = int(os.environ.get("AUXILIARY_DATA_TIMEOUT_S", "120"))
STATE_LOCK_WAIT_S = float(os.environ.get("DATA_UPDATE_LOCK_WAIT_S", "15"))
POSTCLOSE_AUXILIARY_SOURCES = ("lhb", "block_trade", "stock_news")
TPLUS1_AUXILIARY_SOURCES = frozenset({"lhb", "block_trade"})


class DataUpdateBusy(RuntimeError):
    """Another process currently owns the update-state transaction."""


def _blocked_auxiliary_result(exc: Exception) -> dict:
    return {
        "evidence_status": "blocked",
        "reasons": [f"{type(exc).__name__}:{str(exc)[:200]}"],
        "coverage": {"requested": None, "received": None, "failed": None},
        "retryable": True,
        "authoritative": False,
        "affects_strategy": False,
    }


def _run_auxiliary_source(source: str, trade_day: str, observed_at: str,
                          symbols: list[str] | None = None) -> dict:
    """Collect one research-only source in a hard-timeout subprocess."""
    command = [
        PY,
        os.path.join(SRC, "auxiliary_data_sync.py"),
        "--source", source,
        "--trade-day", trade_day,
        "--observed-at", observed_at,
        "--output-root", os.path.join(_BASE, "data", "auxiliary"),
    ]
    symbols_path = None
    try:
        if symbols is not None:
            fd, symbols_path = tempfile.mkstemp(
                prefix="auxiliary_symbols_", suffix=".json"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(list(symbols), handle, ensure_ascii=False)
            command += ["--symbols-json", symbols_path]
        proc = subprocess.run(
            command,
            cwd=_BASE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=AUXILIARY_TIMEOUT_S,
        )
        payload = None
        for line in reversed((proc.stdout or "").splitlines()):
            try:
                payload = json.loads(line)
                break
            except (TypeError, ValueError):
                continue
        if isinstance(payload, dict) and payload.get("evidence_status"):
            payload.setdefault("authoritative", False)
            payload.setdefault("affects_strategy", False)
            payload.setdefault(
                "retryable",
                payload["evidence_status"] in {
                    "pending_publication", "missing", "blocked"
                },
            )
            payload["process_exit"] = proc.returncode
            return payload
        raise RuntimeError(
            f"auxiliary collector exit={proc.returncode}; "
            f"stderr={(proc.stderr or '')[-200:]}"
        )
    except Exception as exc:  # timeout/process failures remain non-authoritative
        return _blocked_auxiliary_result(exc)
    finally:
        if symbols_path:
            try:
                os.unlink(symbols_path)
            except OSError:
                pass


def _tracked_orderbook_symbols(trade_day: str) -> list[str]:
    """Read only same-day positions/snapshot identities for the quote sidecar."""
    compact = trade_day.replace("-", "")
    symbols: set[str] = set()
    snapshot = os.path.join(
        _BASE, "data", "daily", compact, f"signal_snapshot_{compact}.json"
    )
    live_state = os.path.join(_BASE, "data", "live_state.json")

    try:
        with open(snapshot, encoding="utf-8") as f:
            payload = json.load(f)
        snapshot_day = str(payload.get("date") or payload.get("trade_day") or "").replace("-", "")
        if snapshot_day == compact:
            weights = payload.get("weights") or payload.get("target_weights") or {}
            if isinstance(weights, dict):
                symbols.update(str(code) for code in weights if code and str(code) != "CASH")
            for row in payload.get("targets") or payload.get("selection") or []:
                if isinstance(row, dict):
                    code = row.get("canon") or row.get("symbol") or row.get("code")
                    if code:
                        symbols.add(str(code))
    except (OSError, ValueError, TypeError):
        pass

    try:
        with open(live_state, encoding="utf-8") as f:
            payload = json.load(f)
        state_day = str(payload.get("day") or payload.get("trade_day") or "").replace("-", "")
        if state_day == compact:
            for row in payload.get("positions") or []:
                if isinstance(row, dict):
                    code = row.get("canon") or row.get("symbol") or row.get("code")
                    if code:
                        symbols.add(str(code))
    except (OSError, ValueError, TypeError):
        pass

    return sorted(symbols)


def run_orderbook_stage(*, state: dict, trade_day: str, observed_at: str,
                        symbols: list[str], stage_runner) -> dict:
    """Capture one non-authoritative orderbook snapshot for explicit symbols."""
    unique_symbols = sorted({str(symbol) for symbol in symbols if symbol})
    if not unique_symbols:
        return {
            "evidence_status": "blocked",
            "reasons": ["no_same_day_tracked_symbols"],
            "coverage": {"requested": 0, "received": None, "failed": None},
            "retryable": False,
            "authoritative": False,
            "affects_strategy": False,
        }

    success = state.setdefault("auxiliary_success_day", {})
    if success.get("orderbook_snapshot") == trade_day:
        return {
            "evidence_status": "available",
            "status": "already_succeeded",
            "retryable": False,
            "authoritative": False,
            "affects_strategy": False,
        }

    try:
        result = dict(stage_runner(
            "orderbook_snapshot", trade_day, observed_at, symbols=unique_symbols
        ) or {})
    except Exception as exc:  # auxiliary evidence must not stop primary data stages
        result = _blocked_auxiliary_result(exc)
    evidence_status = str(result.get("evidence_status") or "invalid")
    if evidence_status == "available":
        success["orderbook_snapshot"] = trade_day
        state.setdefault("auxiliary_retryable", {}).pop("orderbook_snapshot", None)
    elif evidence_status in {"pending_publication", "missing", "blocked"}:
        state.setdefault("auxiliary_retryable", {})["orderbook_snapshot"] = observed_at
    result.update({
        "retryable": evidence_status in {"pending_publication", "missing", "blocked"},
        "authoritative": False,
        "affects_strategy": False,
    })
    return result


def run_auxiliary_stages(*, state: dict, trade_day: str, observed_at: str,
                         stage_runner,
                         throttle_seconds: int = FAIL_THROTTLE_S) -> dict:
    """Run non-authoritative post-close sources with per-source idempotency.

    The caller supplies the stage runner so this orchestration remains testable
    without network access.  Orderbook is intentionally excluded: it has a
    separate in-session snapshot window and cannot be recreated after close.
    """
    success = state.setdefault("auxiliary_success_day", {})
    retryable = state.setdefault("auxiliary_retryable", {})
    pending_days = state.setdefault("auxiliary_pending_days", {})
    output = {}
    current = datetime.fromisoformat(observed_at)
    for source in POSTCLOSE_AUXILIARY_SOURCES:
        source_pending = sorted(set(pending_days.get(source) or []))
        attempt_day = (
            source_pending[0]
            if source in TPLUS1_AUXILIARY_SOURCES and source_pending
            else trade_day
        )
        if success.get(source) == attempt_day:
            output[source] = {
                "status": "already_succeeded",
                "evidence_status": "available",
                "retryable": False,
                "attempted_trade_day": attempt_day,
            }
            continue
        last_retry = retryable.get(source)
        if last_retry:
            try:
                elapsed = (current - datetime.fromisoformat(last_retry)).total_seconds()
            except (TypeError, ValueError):
                elapsed = throttle_seconds
            if elapsed < throttle_seconds:
                output[source] = {
                    "status": "retry_throttled",
                    "evidence_status": "blocked",
                    "retryable": True,
                    "next_retry_after_seconds": max(0, int(throttle_seconds - elapsed)),
                    "authoritative": False,
                    "affects_strategy": False,
                    "attempted_trade_day": attempt_day,
                }
                continue
        try:
            result = stage_runner(source, attempt_day, observed_at)
        except Exception as exc:  # auxiliary evidence must not stop primary data stages
            result = _blocked_auxiliary_result(exc)
        evidence_status = str((result or {}).get("evidence_status") or "invalid")
        is_success = evidence_status in {"available", "not_applicable"}
        is_retryable = evidence_status in {
            "pending_publication", "missing", "blocked"
        }
        output[source] = {
            **dict(result or {}),
            "status": "complete" if is_success else evidence_status,
            "retryable": is_retryable,
            "authoritative": False,
            "affects_strategy": False,
            "attempted_trade_day": attempt_day,
        }
        if is_success:
            success[source] = attempt_day
            retryable.pop(source, None)
            if source in TPLUS1_AUXILIARY_SOURCES:
                pending_days[source] = [
                    day for day in source_pending if day != attempt_day
                ]
        elif is_retryable:
            retryable[source] = observed_at
            if source in TPLUS1_AUXILIARY_SOURCES:
                pending_days[source] = sorted(set([*source_pending, attempt_day]))
    return output


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
    fd, temporary_name = tempfile.mkstemp(
        prefix=".data_update_last.", suffix=".tmp", dir=os.path.dirname(MARK)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_name, MARK)
    finally:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass


@contextmanager
def _state_file_lock(wait_seconds: float = STATE_LOCK_WAIT_S):
    lock_path = f"{MARK}.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + max(0.0, wait_seconds)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise DataUpdateBusy("another round owns state lock") from exc
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise DataUpdateBusy("another round owns state lock") from exc
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=child_env,
            timeout=BACKFILL_TIMEOUT_S,
        )
        result = {
            "ok": False,
            "exit": proc.returncode,
            "process_ok": proc.returncode == 0,
            "interpreter": exe,
            "missing_days": days,
            "out_fp": out_fp,
            "written": [],
            "timeout_s": BACKFILL_TIMEOUT_S,
            "output_tail": (proc.stdout or "")[-2000:],
        }
        try:
            with open(out_fp, encoding="utf-8-sig") as f:
                full = json.load(f)
            result["written"] = full.get("written") or []
            result["verify"] = full.get("verify")
            result["child_ok"] = full.get("ok")
            result["days"] = full.get("days")
            result["fetch_failed"] = full.get("fetch_failed")
            result["provenance_fp"] = full.get("provenance_fp")
            requested = {day.replace("-", "") for day in days}
            written = {str(day).replace("-", "")
                       for day in result["written"]}
            verified = (isinstance(result["verify"], dict)
                        and result["verify"].get("ok") is True)
            child_success = full.get("ok") is True
            artifact_complete = requested.issubset(written) and verified
            result["ok"] = artifact_complete and (
                proc.returncode == 0 or child_success
            ) and full.get("ok") is not False
            if proc.returncode != 0:
                result["process_warning"] = (
                    "child exited nonzero after writing a verified success result"
                    if result["ok"] else
                    f"child exited nonzero and success evidence is incomplete (exit={proc.returncode})"
                )
        except Exception as exc:  # noqa: BLE001
            result["read_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return result
    except subprocess.TimeoutExpired as exc:
        captured = exc.stdout or exc.output or ""
        if isinstance(captured, bytes):
            captured = captured.decode("utf-8", errors="replace")
        return {
            "ok": False,
            "error": (f"TimeoutExpired: Baostock 回补超过 {BACKFILL_TIMEOUT_S}s"),
            "interpreter": exe,
            "missing_days": days,
            "out_fp": out_fp,
            "written": [],
            "timeout_s": BACKFILL_TIMEOUT_S,
            "output_tail": str(captured)[-2000:],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "interpreter": exe,
            "missing_days": days,
            "out_fp": out_fp,
            "written": [],
            "timeout_s": BACKFILL_TIMEOUT_S,
            "output_tail": "",
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


def _run_round_body(force: bool = False) -> dict:
    st = _state()
    today = datetime.now().strftime("%Y-%m-%d")
    t0 = _now_min()
    out = {}

    def after(T: int) -> bool:
        return force or t0 >= T

    observed_at = datetime.now().astimezone().isoformat()

    # 交易时段末的跟踪标的盘口快照。强制单轮也不在收盘后伪造盘口。
    if T_ORDERBOOK <= t0 < T_ORDERBOOK_END:
        out["orderbook_snapshot"] = run_orderbook_stage(
            state=st,
            trade_day=today,
            observed_at=observed_at,
            symbols=_tracked_orderbook_symbols(today),
            stage_runner=_run_auxiliary_source,
        )

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
    # 旁路研究数据。每源独立回执/幂等，不写 H5i，不参与策略。
    if t0 >= T_AUX_RESEARCH:
        out["auxiliary"] = run_auxiliary_stages(
            state=st,
            trade_day=today,
            observed_at=observed_at,
            stage_runner=_run_auxiliary_source,
        )
    if force or t0 >= T_CLOSE_HINT:
        out["_note"] = "收盘主流程由 daemon.py 在收盘窗口执行 (见 daemon.py)"
    # Persist the complete round receipt so retries and the dashboard can
    # distinguish a primary sync, a fallback backfill, and a blocked attempt.
    # ``default=str`` keeps third-party date-like values auditably serializable
    # without changing the execution result or any production state.
    st["last_round_at"] = datetime.now().isoformat()
    st["last_round"] = json.loads(json.dumps(out, ensure_ascii=False, default=str))
    if "daily" in st["last_round"]:
        st["daily_last"] = st["last_round"]["daily"]
    _save(st)
    return out


def run_round(force: bool = False) -> dict:
    try:
        with _state_file_lock():
            return _run_round_body(force=force)
    except DataUpdateBusy as exc:
        return {
            "_status": "already_running",
            "retryable": True,
            "reason": str(exc),
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.once:
        rep = run_round(force=True)
        # `ensure_ascii` keeps one-shot output printable under legacy Windows
        # console encodings even when result strings contain arrows or paths.
        print(json.dumps(rep, ensure_ascii=True, indent=2, default=str))
        if rep.get("_status") == "already_running":
            raise SystemExit(3)
        return
    _log(f"启动 (interval={INTERVAL_S}s) 窗口: 日线15:45 / 估值16:40 / 收盘由主daemon")
    while True:
        try:
            rep = run_round()
            for k, v in rep.items():
                if isinstance(v, dict) and "ok" in v:
                    if k == "daily":
                        tail = json.dumps(v, ensure_ascii=False, default=str)[-1200:]
                    else:
                        tail = (v.get("out") or v.get("err") or "")[:90]
                    _log(f"{k}: ok={v['ok']} ({tail})")
        except Exception as e:  # noqa: BLE001
            _log(f"轮询异常: {e}")
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
