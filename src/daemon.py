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

#: 数据流动看门狗(#4)的上次结论, 用于"只在结论变化/每 5 分钟"记日志, 避免刷屏。
#: 不落盘: 守护重启后重新判定一次即可, 无需跨进程记忆。
_FLOW_LAST: dict = {}

#: 行情引擎(stockdb.exe)运行期巡检的上次结论 —— 同样只在变化/每 5 分钟记日志。
_STOCKDB_LAST: dict = {}


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
    """探测 pid 进程是否存活（**保守**: 无法判定时按存活处理）。

    [2026-09-21] 实现已抽到 `src/proc_alive.py` 单一事实源 —— 面板此前自己写了一份,
    缺了下面那条退出码校验、又把"打不开"当"已死", 于是把活着的进程报成"未存活"。
    抽公共实现时**保持**本处语义: 未知一律按存活, 以免探测受限时反复重建健康进程
    (那正是守护最该避免的"重启风暴")。
    """
    from proc_alive import alive as _alive
    return _alive(pid, unknown_means_alive=True)


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
    p = subprocess.Popen([PY, os.path.join(_BASE, "src", "realtime_engine.py"),
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
        with open(ENGINE_PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(p.pid))
    except Exception:
        pass
    # [2026-09-21] 不校验存活就记「已启动」是**假成功**的同款隐患(见 P1-DASHRESTORE)。
    # 这里控制流保持不变(仍写 state/pidfile, 下一轮 _proc_alive 判死会自动重试),
    # 只把**日志说法**改成实话, 以便"启动即崩"在日志里看得见而不是被成功掩盖。
    if not _proc_alive(p.pid):
        _log(f"{day} 启动盘中引擎失败: pid={p.pid} 启动后立即退出(见 {ENGINE_LOG}); 下一轮将自动重试")
    else:
        _log(f"{day} 开盘前 | 启动盘中引擎 pid={p.pid} (刷新实时快照, 盘中自动撮合/午间重选)")
    _write_state()


def _ensure_dashboard():
    """确保 Web 可视化运行于 localhost:8000(实时日志/持仓/快照). 未运行则拉起."""
    try:
        pid = None
        if os.path.exists(DASH_PIDFILE):
            pid = int(open(DASH_PIDFILE, encoding="utf-8").read().strip())
        if pid and _proc_alive(pid):
            return True
        out = open(DASH_LOG, "a", encoding="utf-8")
        # [2026-09-21 修] 原为 os.path.join(_BASE, "dashboard.py") —— **该文件不存在**,
        # dashboard.py 在 src/ 下。Popen 对不存在的脚本不抛异常(解释器起来后自己报错退出),
        # 于是本函数会把一个**已死的 pid** 写进 pidfile 并记「已拉起」= 假成功;
        # 面板一旦挂掉就永远拉不回来, 日志还一片"成功"。改为真实路径, 并**校验存活**再报成功。
        _dash_py = os.path.join(_BASE, "src", "dashboard.py")
        if not os.path.exists(_dash_py):
            _log(f"拉起 Web 可视化失败: 脚本不存在 {_dash_py}")
            return False
        p = subprocess.Popen([PY, _dash_py, "--port", str(DASH_PORT)],
                             cwd=_BASE, stdout=out, stderr=out,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        # 等一拍再确认真的活着: 否则"启动即崩"(缺依赖/端口占用)会被记成成功
        time.sleep(1.5)
        if not _proc_alive(p.pid):
            _log(f"拉起 Web 可视化失败: pid={p.pid} 启动后立即退出(见 {DASH_LOG})")
            return False
        if os.path.exists(DASH_PIDFILE):
            try:
                os.remove(DASH_PIDFILE)
            except Exception:
                pass
        try:
            # [2026-09-22, P1-PIDFILE-MULTIWRITER] 改**原子替换**（写 tmp + os.replace）。
            # 原先是"先 remove 再 write": 两步之间存在**空窗期**, 期间 pidfile 不存在,
            # 而任何把"文件不存在"解读为"面板未运行"的代码都会在那一刻去拉一个新实例
            # (实测 2026-09-21 23:36 出现过这个空窗)。os.replace 同盘原子, 读者要么看到旧
            # 内容、要么看到新内容, 不存在"没有文件"的瞬间。
            _tmp = DASH_PIDFILE + ".tmp"
            with open(_tmp, "w", encoding="utf-8") as f:
                f.write(str(p.pid))
            os.replace(_tmp, DASH_PIDFILE)
        except Exception:
            pass
        _log(f"Web可视化未运行, 已拉起 pid={p.pid} -> http://localhost:{DASH_PORT}/")
        return True
    except Exception as e:
        _log(f"拉起 Web 可视化失败: {e}")
        return False


# -------------------------------------------------------------------------
# 观测栈托管 (redis / prometheus / grafana / alertmanager / metrics / celery)
# 2026-09-08: 数据库监控模块 (db_stats/Celery/Prometheus) 挂入本守护周期自愈.
# -------------------------------------------------------------------------
_OBS_DIR = os.path.normpath(os.path.join(_BASE, "..", "obs-stack"))
# 观测栈总开关 (ASTOCK_OBS_STACK=0|false|no 关闭)。默认开 —— 与既有行为一致。
_OBS_ENABLED = os.environ.get("ASTOCK_OBS_STACK", "1").strip().lower() not in ("0", "false", "no")
_OBS_ALERT_HOOK = os.path.join(_BASE, "ops", "alert_hook.py")

#: 死手开关上次判定(档位 + 时刻)。用于"只在变化时记日志 + 30 分钟心跳"。
_DEADMAN_LAST: dict = {}


def _norm_path(p: str) -> str:
    """把命令行里的路径统一成小写 + 正斜杠, 便于**按整段路径**比较而非子串比较。"""
    return str(p or "").replace("\\", "/").lower()


def _cmd_has_script(cmdline: list, script_rel: str) -> bool:
    """命令行的**某个 argv 项**是否**就是**本仓的这个脚本(而非"某处包含这几个字")。

    为什么必须按 argv 逐项比: 2026-09-22 实测, `_obs_procs()` 把三类**完全无关**的
    进程认成了观测栈组件 ——
      · 一个 `node.exe`(DSH 会话本体, 命令行里含本仓路径);
      · `powershell.exe -Command ...`(命令行里含被执行的脚本正文);
      · 我自己临时的 `python -c "..."` 诊断脚本(正文里提到了 metrics_server)。
    它们都因为"命令行里出现了这几个字"而命中, 于是 `metrics in run` 恒为真 ⇒
    **`_ensure_obs_stack()` 永远不会发现 9101 的 metrics_server 已经死了**,
    而真实进程早已不存在。这与 2026-09-21 那次 `ivms320-redis-server` 是同一条通病:
    **子串匹配无法区分"同名/提及"与"就是它"**。上次只补了"要求出现本仓目录",
    这次要补的是"**要求它出现在 argv 的脚本位置上**"。
    """
    want = _norm_path(os.path.join(_BASE, script_rel))
    for tok in cmdline or []:
        if _norm_path(tok) == want:
            return True
    return False


def _obs_procs() -> dict:
    """按进程名/命令行识别观测栈各组件 pid.

    [2026-09-21 修] 原判据是**纯名字子串**匹配, 实测被第三方进程误命中:
    本机有一个 `ivms320-redis-server`(海康威视监控软件自带的 redis), 名字里含
    `redis-server` ⇒ `_obs_procs()` 误以为观测栈的 redis 在跑 ⇒ **从不启动真正的 redis**
    ⇒ celery 连不上 127.0.0.1:6379, 每约 55 分钟起一次又退出(日志表现为 celery 反复"已拉起")。
    这与"诊断脚本匹配到自己"是同一类通病: **子串匹配无法区分'同名'与'同一个'**。

    [2026-09-22 二次修] 上次只加到"要求命令行出现本仓目录", **不够**: 任何**提到**
    本仓路径的进程都会命中(本次实测: node.exe 的 DSH 会话、powershell -Command、
    以及我自己的 `python -c` 诊断脚本)。后果是 `metrics` 恒判为"在跑", 而 9101 上
    的 metrics_server 实际早已不存在 ⇒ **看护永不拉起它**, 告警链(夜里唯一会叫的人)
    静默断掉。故脚本类组件改为**按 argv 逐项精确比对脚本路径**(见 `_cmd_has_script`),
    并要求解释器确实是 python; 原生组件仍按"进程名 + 观测栈目录"判(它们的 exe 名是
    唯一的, 不存在"被提及"的问题)。
    """
    try:
        import psutil
    except Exception:
        return {}
    obs = _OBS_DIR.lower()
    out: dict[str, int] = {}
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            nm = (p.info.get("name") or "").lower()
            cl = p.info.get("cmdline") or []
            cll = " ".join(cl).lower()
        except Exception:
            continue
        is_py = nm.startswith("python") or nm.startswith("pythonw")
        if ("redis-server" in nm or "redis-server" in cll) and obs in cll:
            out.setdefault("redis", p.pid)
        elif "alertmanager" in nm and obs in cll:
            out.setdefault("alertmanager", p.pid)
        elif "prometheus" in nm and "promtool" not in nm and obs in cll:
            out.setdefault("prom", p.pid)
        elif "grafana-server" in nm and obs in cll:
            out.setdefault("grafana", p.pid)
        elif is_py and _cmd_has_script(cl, os.path.join("src", "metrics_server.py")):
            out.setdefault("metrics", p.pid)
        elif is_py and _cmd_has_script(cl, os.path.join("ops", "alert_hook.py")):
            out.setdefault("hook", p.pid)
        elif is_py and "-m" in cl and "celery" in cll and "flower" in cll \
                and "worker" not in cll:
            out.setdefault("flower", p.pid)
        elif is_py and "-m" in cl and "celery" in cll and "worker" in cll:
            out.setdefault("celery", p.pid)
    return out


def _start_obs_component(name: str, cmd: list[str], cwd: str = None) -> bool:
    try:
        p = subprocess.Popen(cmd, cwd=cwd or _BASE,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        # [2026-09-21] 必须校验存活再报成功。背景: 本函数曾把"已拉起"当成功, 而
        # `_obs_procs()` 识别不到刚起的进程时, 每轮(5 分钟)会把 8 个组件**全部重启一遍**,
        # 实测堆积 48 个 alert_hook(见 _ensure_obs_stack docstring)。不校验存活, 这类
        # "起了但立刻死/认不出"的循环就会在日志里显示为一片成功。
        time.sleep(1.0)
        if not _proc_alive(p.pid):
            _log(f"观测栈 {name} 拉起失败: pid={p.pid} 启动后立即退出(见 logs/)")
            return False
        _log(f"观测栈 {name} 未运行, 已拉起 pid={p.pid}")
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"拉起观测栈 {name} 失败: {e}")
        return False


def _check_flow() -> None:
    """数据流动看门狗（路线图 #4）：引擎**活着**但数据停流时报警。

    与 pid 监护互补：`_proc_alive` 抓不到"进程活着、主循环卡住/行情源冻住"这类静默停摆。
    **只报告, 不擅自重启** —— 重启一个活着的引擎可能丢掉它正持有的状态, 属高风险动作,
    按既定红线应转人工处置（阈值与归因分类见 `flow_watchdog.py` 的 docstring）。

    不刷屏：结论变化时记一条, 持续期间每 5 分钟复述一次 —— 否则每 15 秒一行会淹没日志,
    而这正是让告警失效的老路。
    """
    try:
        import flow_watchdog as FW
        r = FW.gather()
    except Exception as e:  # noqa: BLE001
        _log(f"数据流动看门狗异常(不影响主循环): {type(e).__name__}: {e}")
        return
    lvl = r.get("level")
    now = time.time()
    if lvl == "OK":
        if _FLOW_LAST.get("level") not in (None, "OK"):
            _log(f"数据流动已恢复(cause={r.get('cause')}): {r.get('reason')}")
        _FLOW_LAST.update(level="OK", ts=now)
        return
    if lvl != _FLOW_LAST.get("level") or (now - float(_FLOW_LAST.get("ts") or 0)) >= 300:
        _log(f"[{lvl}] 数据流动看门狗: {r.get('reason')}")
        _FLOW_LAST.update(level=lvl, ts=now)


def _probe_stockdb_port(timeout: float = 1.5) -> dict:
    """**廉价**探活: 行情引擎端点是否在监听(纯 TCP connect, 不发协议请求)。

    为什么不用 `engine_bars_sync --probe`: 那个探针要查参考股全历史, 实测 **1.71s**
    （见 `_publish_health_state` 的注释）。启动闸门付得起这个代价, 但**运行期每 5 分钟
    一次的巡检付不起** —— 守护主循环还要看护引擎与观测栈。

    「端口开着」不等于「SDK 能取数」（P1-ENGINEDEP: 引擎在跑但库打不开时, 摄入照样
    返回 0 行, 与『今天没数据』无法区分）。故这里只作为**快速失联判据**:
    端口都不在, 一定是死的; 端口在, 再由下游探针/健康快照去确认能不能取数。
    这个分工是有意的 —— 廉价信号抓"确定死了", 昂贵信号抓"活着但无用"。
    """
    import socket
    try:
        from config import PAPER as _P  # noqa: F401  仅为确认 config 可导入
    except Exception:  # noqa: BLE001
        pass
    host, port = "127.0.0.1", 7899
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"listening": True, "endpoint": f"{host}:{port}", "error": None}
    except Exception as e:  # noqa: BLE001
        return {"listening": False, "endpoint": f"{host}:{port}",
                "error": f"{type(e).__name__}: {e}"}


def _check_deadman() -> None:
    """独立求值死手开关, 并把结论写进**守护日志**(离线可读)。

    为什么要有这一段(而不是只靠 `health_state.gather()`):
      · `gather()` 是**发布者**在采集, 而发布者就是本守护自己, 且只在
        5 分钟看护块里跑 —— 用一个"由被监控者自己执行、还要等 5 分钟"的判据
        去发现"被监控者已经不在了", 原理上不成立;
      · 日志是**离线可读**的: 即使告警链(Prometheus → Alertmanager → hook)
        整条都死了, 事后翻 `daemon.log` 仍能看到"那一刻某组件已经 N 小时没 tick"。
      2026-09-22 实测正是"监测者与被监测者一起消失"(机器 16:08 重启),
      别的机制都没留下任何痕迹。

    **只在档位变化时记一条, 每 30 分钟补一条心跳** —— 每分钟记一次会把
    日志淹掉, 而"淹没的真告警"与"没有告警"等价。
    """
    global _DEADMAN_LAST
    try:
        import deadman_switch as _DMS
        v = _DMS.verdict()
    except Exception as e:  # noqa: BLE001
        # 判不出来就说判不出来, 不静默当健康
        lvl, reasons = "UNKNOWN", [f"死手开关无法求值: {type(e).__name__}: {e}"]
        v = {}
    else:
        lvl = str(v.get("level"))
        reasons = [str(x) for x in (v.get("reasons") or [])]
    now_ts = time.time()
    changed = lvl != _DEADMAN_LAST.get("level")
    due = (now_ts - float(_DEADMAN_LAST.get("at") or 0)) >= 1800
    if changed or due:
        # 心跳也记: 否则"没消息"与"没在查"无法区分(本仓反复踩的那条)
        kinds = {i.get("component"): i.get("status") for i in (v.get("items") or [])}
        _log(f"死手开关 level={lvl} {'(档位变化) ' if changed else '(30 分钟心跳) '}"
             f"items={kinds}"
             + (f" | {'; '.join(reasons)[:200]}" if reasons else ""))
    _DEADMAN_LAST = {"level": lvl, "at": now_ts}


def _ensure_stockdb() -> None:
    """行情引擎(stockdb.exe)**运行期**巡检: 掉了就响亮报警。

    为什么必须有这一段（2026-09-22 的真实事故, 见登记册 P0-DATASRC-STOCKDB）:
    `ops/start_daemon.ps1` 早就有启动闸门（探不到引擎就 exit 4 拒绝启动）——
    也就是说**启动时**是挡得住的。但当天的问题是:
      ① 守护 16:09 才启动（上午根本没人看护）;
      ② 更根本的是 —— **没有任何东西在运行期发现引擎掉了**。11:31 引擎失联后,
         守护如果活着, 也只会继续看护一个注定拿不到数据的引擎, 直到收盘才发现
         "今天没有新数据", 而这与"今天是节假日"**无法区分**。
    启动闸门管不住运行期, 这一段补的就是那个缺口。

    **只报告, 不擅自拉起** —— 与 `_check_flow` 同一条纪律, 理由更强:
    引擎掉线通常意味着它的 leveldb 坏了或更新器没跑, 重启只会得到一台
    能连上但取不到数的引擎（P1-ENGINEDEP 的原话: 症状与"今天没数据"无法区分）。
    处置应转人工。而**机器重启后的自动拉起**由 SCM 负责（AStockStockdb 是
    `SERVICE_AUTO_START` + `AppExit Restart`）—— 那才是"该自动"的那一半。

    不刷屏: 结论变化时记一条, 持续期间每 5 分钟复述一次（与本循环的看护节奏一致）。
    """
    try:
        r = _probe_stockdb_port()
    except Exception as e:  # noqa: BLE001
        _log(f"行情引擎巡检异常(不影响主循环): {type(e).__name__}: {e}")
        return
    listening = bool(r.get("listening"))
    lvl = "OK" if listening else "CRITICAL"
    now = time.time()
    if lvl == "OK":
        if _STOCKDB_LAST.get("level") not in (None, "OK"):
            _log(f"行情引擎已恢复监听({r.get('endpoint')}) —— 数据摄入链路可用")
        _STOCKDB_LAST.update(level="OK", ts=now)
        return
    if lvl != _STOCKDB_LAST.get("level") or (now - float(_STOCKDB_LAST.get("ts") or 0)) >= 300:
        _log(f"[{lvl}] 行情引擎 {r.get('endpoint')} **不在监听** ({r.get('error')})")
        _log(f"       后果: 盘中引擎取不到数会静默停摆, 且症状与『今天没数据』无法区分。")
        _log(f"       处置(转人工, 不自动重启): 检查服务 AStockStockdb "
             f"(`Get-Service AStockStockdb`); 若已停止, 查 {LOG_DIR}\\stockdb_service.err.log "
             f"与 E:\\A_stockDB\\log.txt (常见: leveldb Corruption / 更新器未跑)。")
        _STOCKDB_LAST.update(level=lvl, ts=now)


def _publish_health_state() -> None:
    """发布运行态健康快照（路线图 #2 的**发布者**, 单一出口）。

    为什么由守护进程发布、面板只读快照:
      引擎探针实测 1.71s（查 4 只参考股全历史）, 而面板前端每 3 秒轮询 /api/health ——
      探针放进请求路径会拖垮面板; 且守护进程以 LocalSystem 运行, `STOCKDB_ROOT` 由
      NSSM `AppEnvironmentExtra` 注入, 环境依赖收敛在这一处即可。

    节奏复用本循环已有的 5 分钟看护（dash_tick>=20）, 不新造计时器。
    **失败绝不影响守护主循环** —— 健康发布挂掉不能连带把引擎看护也拖死。
    """
    try:
        import health_state
        r = health_state.publish()
        _log(f"健康快照已发布: state={r.get('state')} reasons={r.get('reasons') or '无'}")
    except Exception as e:  # noqa: BLE001
        _log(f"健康快照发布失败(不影响守护主循环): {type(e).__name__}: {e}")


def _ensure_obs_stack() -> None:
    """崩溃自愈: 每轮周期检查观测栈组件, 缺谁拉起谁.

    [2026-09-21] 加总开关。事故: `.venv310` 缺 `psutil` ⇒ `_obs_procs()` **恒返回空字典**
    ⇒ 每个组件都被判"未运行" ⇒ 每轮(约 5 分钟)把 8 个组件**全部重启一遍**; 其中
    `alert_hook` 新起的实例不退出, 实测堆积 **48 个**、并持续以 ~1 个/5 分钟增长。
    补齐 psutil 后判据恢复(`_obs_procs()` 返回非空), 循环即停。
    保留开关的意义: 让"不想要观测栈"的部署能**干脆关掉**, 而不是靠"碰巧缺包"来关掉 ——
    后者既不可见也不可靠。
    """
    if not _OBS_ENABLED:
        return
    try:
        run = _obs_procs()
        missing = []
        if "redis" not in run:
            _start_obs_component("redis", [os.path.join(_OBS_DIR, "redis", "redis-server.exe")],
                                 cwd=os.path.join(_OBS_DIR, "redis"))
        if "prom" not in run:
            prom_exe = os.path.join(_OBS_DIR, "prom", "prometheus-2.53.2.windows-amd64", "prometheus.exe")
            if os.path.exists(prom_exe):
                _start_obs_component("prometheus",
                                     [prom_exe, "--config.file=prometheus.yml",
                                      "--storage.tsdb.path=prom/data"],
                                     cwd=_OBS_DIR)
        if "grafana" not in run:
            g_home = os.path.join(_OBS_DIR, "grafana", "grafana-v11.1.0")
            g_exe = os.path.join(g_home, "bin", "grafana-server.exe")
            if os.path.exists(g_exe):
                _start_obs_component("grafana", [g_exe, "--homepath", g_home, "server"],
                                     cwd=g_home)
        if "alertmanager" not in run:
            am_exe = os.path.join(_OBS_DIR, "alertmanager", "alertmanager.exe")
            if os.path.exists(am_exe):
                _start_obs_component("alertmanager",
                                     [am_exe, "--config.file=alertmanager.yml",
                                      "--storage.path=am/data"],
                                     cwd=_OBS_DIR)
        if "hook" not in run and os.path.exists(_OBS_ALERT_HOOK):
            _start_obs_component("alert-hook",
                                 [PY, _OBS_ALERT_HOOK, "--port", "9111"])
        if "flower" not in run:
            _start_obs_component("flower",
                                 [PY, "-m", "celery", "-A", "src.tasks_db",
                                  "flower", "--port=5555"])
        if "metrics" not in run:
            _start_obs_component("metrics",
                                 [PY, os.path.join(_BASE, "src", "metrics_server.py"),
                                  "--port", "9101"])
        if "celery" not in run:
            _start_obs_component("celery",
                                 [PY, "-m", "celery", "-A", "src.tasks_db", "worker",
                                  "--pool=solo", "-E", "-l", "warning",
                                  "--without-gossip", "--without-mingle",
                                  "--without-heartbeat"])
    except Exception as e:  # noqa: BLE001
        _log(f"观测栈托管异常: {e}")


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
    # 启动即发布一次健康快照: 否则要等第一个 5 分钟看护轮次, 面板在这段窗口里会显示
    # "快照陈旧" —— 那是**发布者还没跑**, 不是系统有病, 不该让人误读。
    _publish_health_state()

    dash_tick = 0  # dashboard 健康检查轮次计数(周期性看护, 兜底外部 keep_alive)
    # [P0 清单第6项] Dead-Man's Switch: 主循环每个自然分钟留一次 tick。
    # 为什么它是既有心跳/看门狗**覆盖不到**的那一块: 心跳是"等组件回应",
    # 看门狗是"观测数据是否流动" —— 两者都要求**监测者自己还活着**。
    # 守护进程自身被杀死时, 心跳文件停更与"组件没在跑"不可区分, 看门狗也不再
    # 产报告。而死手开关判的是"本该出现的 tick 没出现", 失联本身即是证据。
    # 阈值 = 3 × 60s = 180s(与 heartbeat/flow_watchdog 的"3×标称周期"房规一致)。
    _dm_last = 0.0

    while True:
        now = datetime.now()
        today = now.date()
        cur_time = now.time()
        day_str = today.strftime("%Y-%m-%d")

        # 死手开关 tick(每分钟一次, 失败静默 —— 留痕不得拖垮守护)
        try:
            import time as _t
            if _t.time() - _dm_last >= 60.0:
                import deadman_switch as _DMS
                if _DMS.beat("daemon", note=f"day={day_str} t={cur_time.strftime('%H:%M')}"):
                    _dm_last = _t.time()
        except Exception:  # noqa: BLE001
            pass

        # 优雅停止
        if os.path.exists(STOP_FILE):
            try:
                os.remove(STOP_FILE)
            except Exception:
                pass
            _log("收到停止请求, 守护主循环退出(引擎子进程保留)")
            break

        # 周期性看护 Web 可视化 + 观测栈: 每 20 轮(约 5 分钟)检查一次, 挂了自动拉起
        dash_tick += 1
        if dash_tick >= 20:
            dash_tick = 0
            _ensure_stockdb()          # [P0-DATASRC-STOCKDB] 行情引擎运行期巡检
            _ensure_dashboard()
            _ensure_obs_stack()
            _publish_health_state()
        # 死手开关判定: **放在看护块之外**, 每个自然分钟都问一次。
        #
        # [2026-09-22] `health_state.gather()` 里也会采集 `verdict()`, 但那是
        # **发布者**在做 —— 而发布者就是本守护自己, 且只在本分支里跑。
        # 用一个"由被监控者自己执行、还要等 5 分钟"的判据去发现
        # "被监控者已经不在了", 是原理上不成立的: 它恰恰在最需要它的时刻不执行。
        # 故这里独立地问一次, 并把结论**同时写进守护日志** ——
        # 日志是**离线可读**的: 即使告警链整条都死了, 事后翻日志仍能看到
        # "那一刻引擎已经 4 小时没 tick 了"。
        _check_deadman()

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
                else:
                    # 进程活着 -> 再问一句"数据还在流吗"(路线图 #4)。
                    # 这是 pid 监护**原理上抓不到**的一类: 主循环卡住/行情源冻住时,
                    # 进程一切正常, 盘面却静静冻住, 直到收盘才发现整天没动。
                    _check_flow()
            elif _state["running_day"] != day_str and _state["engine_pid"]:
                # [2026-09-22 修] **自愈不能依赖内存态 `running_day`**。
                # 上面那个 elif 把崩溃自愈 gate 在"本进程记得今天起过引擎"上, 而
                # `running_day` 只在 `_start_engine()` 里被赋值 —— **daemon 自己一重启,
                # 这个记忆就没了**, 自愈分支永不进入, 且因为 `_log` 就在被跳过的分支里,
                # **连一句日志都不会产生**。
                # 当天实测: 机器 16:08:42 重启, daemon 16:09:07 起(恢复的 running_day=null),
                # 引擎最后一次写 12:29:35 ⇒ 12:29:35~16:08 这段 daemon 活着、引擎已死,
                # **零次自愈尝试、零条日志**, 只能靠比对 mtime 考古才发现。
                # 而那恰恰是最需要自愈的场景: 守护自己刚重启过。
                #
                # 修法: 用**可观测事实**(现在是不是该有引擎)替代**易失的内存记忆**。
                # `engine_pid` 仍在 state 里(持久化过), 就直接问它活不活。
                #
                # [2026-09-22 二次修 —— 首版自己踩的坑, 留档] 首版**漏了时间闸门**,
                # 于是收盘后每次循环都 `_start_engine`: 引擎起来一看已过 15:05, 自己
                # 立刻退出(`已收盘或非交易日, 引擎自动停止`), 30 秒后守护又拉一次 ——
                # **重启风暴**, 实测 20:12~20:14 每 30 秒一个 pid(18956/8020/11608/19396)。
                # 这正是 `_proc_alive` docstring 里点名要避免的那种事。
                # 教训: 加一条自愈路径时, 必须把**原路径的所有前置条件**都复制过来 ——
                # 时间闸门不是装饰, 它和存活判断同等重要。
                if cur_time >= dtime(15, 3):
                    # 已过收盘窗口: 引擎本就该停, 不是故障。清掉会话记忆即可, 不重启。
                    _state["running_day"] = None
                    _state["engine_done"] = True
                    _write_state()
                elif not _proc_alive(_state["engine_pid"]):
                    _log(f"引擎不在运行(pid={_state['engine_pid']}, "
                         f"running_day={_state['running_day']!r}) —— 自愈拉起")
                    _start_engine(today)
                else:
                    # 进程确实活着, 只是本进程不记得 —— 把记忆补上, 免得下一轮又走这里
                    _state["running_day"] = _state.get("running_day") or day_str
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
            dp = int(open(DAEMON_PID, encoding="utf-8").read().strip())
            print(f"  守护pid    : {dp} ({'运行中' if _proc_alive(dp) else '已停止'})")
        except Exception:
            pass
    # Web 可视化状态
    try:
        dp = int(open(DASH_PIDFILE, encoding="utf-8").read().strip())
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
        dp = int(open(DAEMON_PID, encoding="utf-8").read().strip())
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
    with open(DAEMON_PID, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    try:
        run_loop()
    finally:
        try:
            os.remove(DAEMON_PID)
        except Exception:
            pass