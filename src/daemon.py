# ============================================================
# daemon.py -- 全A轮动模拟盘 常驻守护进程 (取代 Windows 计划任务)
#
# 职责: 每个交易日按时间表自动调度, 免 OS 计划任务依赖, 崩溃自动拉起。
#   同时托管 Web 可视化(localhost:8000, 实时日志/持仓/快照)。
#   时间表(交易日 周一~周五):
#     08:50  启动盘中实时撮合引擎 (realtime_engine.py --interval 15)
#     11:20  引擎内部自动午间重选(引擎自带, 守护不介入)
#     11:30-13:00 午休(引擎tick仍跑, 不撮合)
#     15:03  引擎 loop() 自行 break 停止
#     15:05  收盘选股 (run_daily.py) 生成次日目标池 + 归档当日快照
#     次日08:30  再次启动引擎, 循环
#   周末(周六/日)不调度, 守护空转等待。
# 日志: 引擎/收盘/守护全部追加到 logs/daemon.log(完整) 并同步到 logs/daemon_tail.log
#       (前端 /api/logs 实时滚动读取最新增量)
# 用法:
#   python daemon.py            # 前台常驻
#   python daemon.py --status   # 查看状态 + 最近日志
#   python daemon.py --stop     # 停止守护(仅停守护, 不杀引擎)
# ============================================================

import os
import sys
import json
import time
import argparse
import subprocess
from datetime import datetime, date, time as dtime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
os.chdir(_BASE)

PY = sys.executable
# 强制使用 TRAE 自带的 Python (含 duckdb / polars / vnpy / sb3 等),
# 避免 PATH 中其它 Python (如系统 Python 3.14) 找不到依赖.
_TRAE_PY = os.environ.get("TRAE_PYTHON", "")  # 可选: 指定含依赖的外部 Python 解释器
if os.path.exists(_TRAE_PY):
    PY = _TRAE_PY

# 静默 akshare 内部的 tqdm 进度条 ("Please wait for a moment: 50% | ...")
# 同时让 duckdb 走子进程模式 (Windows 下更稳定)
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("AKSHARE_TQDM", "0")

LOG_DIR = os.path.join(_BASE, "logs")
DAEMON_PID = os.path.join(LOG_DIR, "daemon.pid")
DAEMON_LOG = os.path.join(LOG_DIR, "daemon.log")
TAIL_LOG = os.path.join(LOG_DIR, "daemon_tail.log")
ENGINE_LOG = os.path.join(LOG_DIR, "live_engine.log")
DASH_LOG = os.path.join(LOG_DIR, "dashboard.log")
ENGINE_PIDFILE = os.path.join(LOG_DIR, "engine.pid")
DASH_PIDFILE = os.path.join(LOG_DIR, "dashboard.pid")
DASH_PORT = 8000
STOP_FILE = os.path.join(LOG_DIR, "daemon.stop")

# 交易日关键节点
MARKET_OPEN = dtime(8, 30)      # 盘前健康检查 + 启动盘中引擎
# [2026-09-08] 收盘选股自 15:05 后移至 19:10: 数据商在收盘后 1-4 小时才完成
# 日线/估值更新, 过早触发会因数据未就绪而失败; data_update_daemon 在 15:45/16:40
# 分窗口补齐数据, 19:10 再跑 run_daily(mode=full) 成功率更高.
MARKET_CLOSE = dtime(19, 10)    # 收盘选股 (数据就绪后)
DONE_WINDOW = dtime(22, 0)      # 收盘任务运行最晚窗口

# 盘前健康检查脚本(与本守护同项目, 用 _TRAE_PY 跑)
PREMARKET_HEALTHCHECK = os.path.join(_BASE, "src", "premarket_healthcheck.py")

# P08 模拟盘收盘汇总(独立于本守护所在项目; 依赖 vnpy venv, 用其自带 python)
P08_SIM_DIR = os.path.normpath(os.path.join(_BASE, "..", "p08-sim-live"))
P08_CLOSE = os.path.join(P08_SIM_DIR, "daily_close.py")
P08_VENV_PY = os.path.normpath(os.path.join(_BASE, "..", "research_trader",
                                            ".venv-vnpy", "Scripts", "python.exe"))

# 运行状态(全局, 供 --status 读取通过 daemon_state.json)
STATE_JSON = os.path.join(LOG_DIR, "daemon_state.json")
_state = {
    "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "running_day": None,        # 当前已启动引擎的交易日 (date str)
    "engine_pid": None,
    "engine_done": False,       # 引擎已退出(可跑收盘)
    "last_close_day": None,     # 最后一次收盘选股的交易日
    "last_maint_day": None,     # 最后一次非交易日维护的日期
    "last_p08_close_day": None,  # 最后一次 P08 收盘汇总已执行的交易日
    "last_health_day": None,     # 最后一次盘前健康检查已执行的交易日
    "last_error": None,
}


def _write_state():
    try:
        with open(STATE_JSON, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass


def _log(txt, sub=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag = "[daemon]" if not sub else f"[{sub}]"
    line = f"{ts} {tag} {txt}"
    for p in (DAEMON_LOG, TAIL_LOG):
        try:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    _trim_tail()
    print(line, flush=True)


def _trim_tail(max_lines=4000, max_bytes=512 * 1024):
    try:
        if os.path.getsize(TAIL_LOG) > max_bytes:
            with open(TAIL_LOG, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > max_lines:
                with open(TAIL_LOG, "w", encoding="utf-8") as f:
                    f.writelines(lines[-max_lines:])
    except Exception:
        pass


def _proc_alive(pid):
    if not pid:
        return False
    try:
        import ctypes
        # PROCESS_QUERY_INFORMATION | SYNCHRONIZE 权限位, 进程结束后 OpenProcess 返回 NULL.
        # 旧代码用 1 (PROCESS_TERMINATE) 会对已结束进程误判为"存活", 使看护不重建崩溃的 dashboard.
        h = ctypes.windll.kernel32.OpenProcess(0x0400, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def _is_trading_day(d):
    """是否为 A 股交易日(含节假日判定, 走权威交易日历)."""
    from trading_calendar import is_trading_day as _tc_day
    return _tc_day(d)


# -------------------------------------------------------------------------
# 子进程启动 / 日志增量同步
# -------------------------------------------------------------------------
_offsets = {}


def _sync_sub_logs():
    """把 引擎/仪表盘/守护 新增日志行同步到 TAIL_LOG(前端滚动读取)."""
    for src in (ENGINE_LOG, DASH_LOG):
        try:
            size = os.path.getsize(src)
            off = _offsets.get(src, 0)
            if size > off:
                with open(src, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(off)
                    new = f.read()
                with open(TAIL_LOG, "a", encoding="utf-8") as t:
                    for ln in new.splitlines():
                        if ln.strip():
                            t.write(f"[{datetime.now():%H:%M:%S}] [sub] {ln}\n")
                _offsets[src] = size
                _trim_tail()
        except Exception:
            _offsets.setdefault(src, 0)


def _start_engine(day: date):
    """启动盘中引擎, 输出重定向到 ENGINE_LOG, 记录 pid."""
    out = open(ENGINE_LOG, "a", encoding="utf-8")
    p = subprocess.Popen([PY, os.path.join(_BASE, "realtime_engine.py"),
                          "--interval", "15"],
                         cwd=_BASE, stdout=out, stderr=out,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    _state["engine_pid"] = p.pid
    _state["running_day"] = day.strftime("%Y-%m-%d")
    _state["engine_done"] = False
    if os.path.exists(ENGINE_PIDFILE):
        try:
            os.remove(ENGINE_PIDFILE)
        except Exception:
            pass
    try:
        with open(ENGINE_PIDFILE, "w") as f:
            f.write(str(p.pid))
    except Exception:
        pass
    _log(f"{day} 开盘前 | 启动盘中引擎 pid={p.pid} (刷新实时快照, 盘中自动撮合/午间重选)")
    _write_state()


def _ensure_dashboard():
    """确保 Web 可视化运行于 localhost:8000(实时日志/持仓/快照). 未运行则拉起."""
    try:
        pid = None
        if os.path.exists(DASH_PIDFILE):
            pid = int(open(DASH_PIDFILE).read().strip())
        if pid and _proc_alive(pid):
            return True
        out = open(DASH_LOG, "a", encoding="utf-8")
        p = subprocess.Popen([PY, os.path.join(_BASE, "dashboard.py"),
                              "--port", str(DASH_PORT)],
                             cwd=_BASE, stdout=out, stderr=out,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        if os.path.exists(DASH_PIDFILE):
            try:
                os.remove(DASH_PIDFILE)
            except Exception:
                pass
        try:
            with open(DASH_PIDFILE, "w") as f:
                f.write(str(p.pid))
        except Exception:
            pass
        _log(f"Web可视化未运行, 已拉起 pid={p.pid} -> http://localhost:{DASH_PORT}/")
        return True
    except Exception as e:
        _log(f"拉起 Web 可视化失败: {e}")
        return False


def _run_daily(day: date, mode: str = "full"):
    """收盘选股 / 非交易日维护.
    mode='full'  : 交易日完整管道(数据拉取+选股+归档+模型训练).
    mode='maint' : 周末/节假日维护管道(仅数据拉取+因子/ArcticDB刷新+模型训练/权重反馈).
    """
    tag = "收盘选股" if mode == "full" else "非交易日维护"
    _log(f"{day} {tag} run_daily.py (mode={mode})")
    try:
        cmd = [PY, os.path.join(_BASE, "src", "run_daily.py")]
        if mode == "maint":
            cmd.append("--maint")
        p = subprocess.Popen(cmd, cwd=_BASE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        # run_daily 整条管道含数据拉取+模型训练(DRL/vnpy), 用时可能长达数十分钟.
        # 旧代码 timeout=300(5分钟) 会过早 terminate 主进程, 使依赖主进程回执的
        # IC权重反馈/绩效/LLM/daily_summary 收尾丢失(IC 产物曾停在 08-24). 放宽到 1 小时.
        try:
            out, _ = p.communicate(timeout=3600)
        except subprocess.TimeoutExpired:
            # 超时后真正确认并回收子进程, 避免残留孤儿训练进程
            ctx = p.kill()
            p.wait(timeout=30)
            _log(f"{day} {tag} 超时(>3600s), 已终止 run_daily 主进程")
            return
        for ln in out.splitlines():
            if ln.strip():
                _log(ln, "daily")
        _log(f"{day} {tag} 完成 exit={p.returncode}")
    except Exception as e:
        _log(f"{day} {tag} 异常: {e}", "daily")
    _state["last_close_day"] = day.strftime("%Y-%m-%d")
    _state["last_maint_day"] = day.strftime("%Y-%m-%d")
    _write_state()


def _run_premarket_healthcheck(day: date):
    """盘前健康检查: 每日开盘前对 数据存储/查询引擎/AI Agent 三层做健康探测.
    等价于健康检查脚本被独立定时触发; 现随本守护按交易日每天一次(08:30 前后),
    用 _TRAE_PY 运行 premarket_healthcheck.py, 结果落盘 data/health/premarket.json."""
    tag = "盘前健康检查"
    _log(f"{day} {tag} premarket_healthcheck.py")
    try:
        cmd = [PY, PREMARKET_HEALTHCHECK]
        p = subprocess.Popen(cmd, cwd=_BASE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        try:
            out, _ = p.communicate(timeout=900)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=30)
            _log(f"{day} {tag} 超时(>900s), 已终止")
            return
        for ln in out.splitlines():
            if ln.strip():
                _log(ln, "health")
        _log(f"{day} {tag} 完成 exit={p.returncode}")
    except Exception as e:
        _log(f"{day} {tag} 异常: {e}", "health")
    _state["last_health_day"] = day.strftime("%Y-%m-%d")
    _write_state()


def _run_p08_close_summary(day: date):
    """P08 模拟盘收盘汇总: 读取 P08 落盘帧聚合出每日回执.
    独立于本守护项目, 依赖 research_trader/.venv-vnpy 的 python 运行
    p08-sim-live/daily_close.py (原由外部定时任务触发, 现并入本守护时间表)."""
    tag = "P08收盘汇总"
    if not os.path.exists(P08_CLOSE):
        _log(f"{day} {tag} 脚本不存在({P08_CLOSE}), 跳过", "p08")
        return
    _log(f"{day} {tag} daily_close.py")
    try:
        py = P08_VENV_PY if os.path.exists(P08_VENV_PY) else PY
        cmd = [py, P08_CLOSE, day.strftime("%Y%m%d")]
        p = subprocess.Popen(cmd, cwd=P08_SIM_DIR, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        try:
            out, _ = p.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=30)
            _log(f"{day} {tag} 超时(>600s), 已终止")
            return
        for ln in out.splitlines():
            if ln.strip():
                _log(ln, "p08")
        _log(f"{day} {tag} 完成 exit={p.returncode}")
    except Exception as e:
        _log(f"{day} {tag} 异常: {e}", "p08")
    _state["last_p08_close_day"] = day.strftime("%Y-%m-%d")
    _write_state()


# -------------------------------------------------------------------------
# 主调度循环
# -------------------------------------------------------------------------
def run_loop():
    _log(f"守护进程启动 pid={os.getpid()}")
    _log(f"交易日时间表: 引擎{MARKET_OPEN:%H:%M}启动 / 收盘{MARKET_CLOSE:%H:%M}选股 | 免OS计划任务 | 崩溃自动拉起")
    _write_state()

    dash_tick = 0  # dashboard 健康检查轮次计数(周期性看护, 兜底外部 keep_alive)

    while True:
        now = datetime.now()
        today = now.date()
        cur_time = now.time()
        day_str = today.strftime("%Y-%m-%d")

        # 优雅停止
        if os.path.exists(STOP_FILE):
            try:
                os.remove(STOP_FILE)
            except Exception:
                pass
            _log("收到停止请求, 守护主循环退出(引擎子进程保留)")
            break

        # 周期性看护 Web 可视化: 每 20 轮(约 5 分钟)检查一次, 挂了自动拉起
        dash_tick += 1
        if dash_tick >= 20:
            dash_tick = 0
            _ensure_dashboard()

        # 非交易日(周末/节假日): 不启动盘中引擎, 不跑收盘选股.
        # 仅在维护窗口(15:05~22:00)每天跑一次 maint 维护管道 = 数据拉取+模型训练.
        if not _is_trading_day(today):
            if now.hour == 0 and now.minute < 1:
                _log(f"[空闲] 非交易日({today}) 维护模式(数据拉取+模型训练)")
            if (dtime(15, 5) <= cur_time <= dtime(22, 0)
                    and _state["last_maint_day"] != day_str):
                _log(f"非交易日维护触发: {today} -> run_daily.py (--maint)")
                _run_daily(today, mode="maint")
            _sync_sub_logs()
            time.sleep(60)
            continue

        # 交易日: 开盘前启动引擎(08:30~15:03); Web 可视化由外部 keep_alive 热看护 + 本守护周期性兜底(见循环开头)
        if MARKET_OPEN <= cur_time < dtime(23, 59):
            # 盘前健康检查: 开盘前窗口(08:30~09:30)每天一次, 先于引擎启动对三层做探活
            if (cur_time <= dtime(9, 30) and _state["last_health_day"] != day_str
                    and not (_state["running_day"] and _proc_alive(_state["engine_pid"]))):
                _run_premarket_healthcheck(today)
            # 启动引擎(本交易日尚未启动 & 尚未收盘)
            if (not _state["running_day"] and _state["last_close_day"] != day_str
                    and cur_time < dtime(15, 3)):
                _start_engine(today)
            elif _state["running_day"] == day_str:
                # 引擎运行中: 检查异常退出 -> 秒速拉起(崩溃保护)
                if not _proc_alive(_state["engine_pid"]):
                    if cur_time < dtime(15, 3):
                        _log(f"引擎异常退出 pid={_state['engine_pid']}, 自动拉起…")
                        _start_engine(today)
                    else:
                        _state["running_day"] = None
                        _state["engine_done"] = True
                        _write_state()
            _sync_sub_logs()
        else:
            _sync_sub_logs()
            time.sleep(30)
            continue

        # 收盘后: 引擎退出 -> 收盘选股(15:05~22:00, 每交易日仅一次)
        if cur_time >= dtime(15, 3):
            if (_state["running_day"] == day_str
                    and _state["engine_pid"] and not _proc_alive(_state["engine_pid"])):
                _log("盘中引擎已退出(收盘)")
                _state["running_day"] = None
                _state["engine_done"] = True
                _write_state()
            if (cur_time >= MARKET_CLOSE and cur_time <= DONE_WINDOW
                    and _state["last_close_day"] != day_str):
                _run_daily(today)
            # P08 模拟盘收盘汇总: 同一收盘窗口(15:05~22:00), 每交易日仅一次
            if (cur_time >= MARKET_CLOSE and cur_time <= DONE_WINDOW
                    and _state["last_p08_close_day"] != day_str):
                _run_p08_close_summary(today)

        time.sleep(15)


def _status():
    print("=== 全A轮动守护进程状态 ===")
    try:
        with open(STATE_JSON, encoding="utf-8") as f:
            st = json.load(f)
        print(f"  启动时间    : {st['started']}")
        print(f"  当日引擎    : {st.get('running_day')}")
        print(f"  引擎pid    : {st.get('engine_pid')} "
              f"({'运行中' if _proc_alive(st.get('engine_pid')) else '已停止'})")
        print(f"  最后收盘日  : {st.get('last_close_day')}")
        print(f"  P08收盘汇总 : {st.get('last_p08_close_day') or '未执行'}")
        print(f"  盘前健康检查: {st.get('last_health_day') or '未执行'}")
        print(f"  最后错误    : {st.get('last_error')}")
    except Exception as e:
        print("  状态文件读取失败:", e)
    if os.path.exists(DAEMON_PID):
        try:
            dp = int(open(DAEMON_PID).read().strip())
            print(f"  守护pid    : {dp} ({'运行中' if _proc_alive(dp) else '已停止'})")
        except Exception:
            pass
    # Web 可视化状态
    try:
        dp = int(open(DASH_PIDFILE).read().strip())
        print(f"  Web可视化  : pid={dp} ({'运行中' if _proc_alive(dp) else '已停止'}) -> http://localhost:{DASH_PORT}/")
    except Exception:
        print(f"  Web可视化  : 未运行 -> http://localhost:{DASH_PORT}/")
    print("\n--- 最近 12 行日志 ---")
    try:
        with open(TAIL_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for l in lines[-12:]:
            print("  " + l.rstrip())
    except Exception:
        print("  无日志")


def _stop():
    if not os.path.exists(DAEMON_PID):
        print("守护进程未在运行(无 pid 文件)")
        return
    try:
        dp = int(open(DAEMON_PID).read().strip())
        if _proc_alive(dp):
            subprocess.run(["taskkill", "/F", "/PID", str(dp)], capture_output=True)
            print(f"守护进程已停止 pid={dp} (引擎子进程自动并存)")
        else:
            print("守护进程 pid 文件存在但进程已退出")
        try:
            os.remove(DAEMON_PID)
        except Exception:
            pass
    except Exception as e:
        print("停止失败:", e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.stop:
        _stop()
        sys.exit(0)
    if args.status:
        _status()
        sys.exit(0)
    # 守护重启时恢复上次持久化的运行状态(daemon_state.json), 否则内存 _state 从默认值
    # (None) 起步, 会把"今天已完成"的收盘选股/非交易日维护/健康检查误判为未做而整条重跑
    # 一次(数据拉取+模型训练可长达几十分钟). 只读恢复, 不做覆盖合并上的数据清洗.
    try:
        with open(STATE_JSON, encoding="utf-8") as f:
            _state.update(json.load(f))
    except Exception:
        pass
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(DAEMON_PID, "w") as f:
        f.write(str(os.getpid()))
    try:
        run_loop()
    finally:
        try:
            os.remove(DAEMON_PID)
        except Exception:
            pass