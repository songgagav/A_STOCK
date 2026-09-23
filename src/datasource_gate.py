# -*- coding: utf-8 -*-
"""数据源健康门禁 (2026-09-22 批次): **数据源死了要响亮停手**。

为什么需要它
------------
2026-09-22 实盘暴露的真实故障链:

  厂商引擎 stockdb.exe 未运行 → 盘中引擎 11:31 静默停摆 → 下午无行情无撮合 →
  **直到收盘才发现"今天没有新数据"** —— 而这与"今天是节假日"**无法区分**。

已有的两道防线各缺一半:
  · `ops/start_daemon.ps1` 有**启动**闸门（探不到引擎 exit 4）—— 只管启动那一刻;
  · `daemon._ensure_stockdb()` 有**运行期**巡检（引擎不在监听就 CRITICAL）——
    只管"端口在不在"。
两者都不管**"服务活着但数据没进来"**, 也就是本模块负责的那一半。

五种失效模式（全部有本仓实测证据, 阈值不拍脑袋）
------------------------------------------------
在写规则之前先把当天观察到的形态列全, 规则逐条对应:

  (a) **供应商不可达** —— `probe_error_kind=unreachable`,
      `TimeoutError: Connect timeout`（实测 2026-09-22 health/state.json）。
  (b) **供应商未发布次日数据** —— 引擎数据落后最后已收盘交易日 1 个交易日
      （实测 2026-09-22: `engine_day=20260918, expected_day=20260921`）。
      这条**当天是合理的**（厂商盘后才发布）, 故只能观察、不能拦 —— 见 `evaluate`
      里 `PUBLISHER_GRACE` 的说明。
  (c) **厂商 SDK 不可用** —— `db_update` 12 张表全 `ok=false, rows=0`, 而
      `akshare_stats.last_status="ok"`（实测 2026-09-22 daily_summary）。
      **症状与"今天没数据"无法区分**, 故这里要求"报错必须带原因"。
  (d) **同步到 0 行但是假成功** —— `engine_bars_sync` 报 `ok=true` 而
      `appended=0`, 同时 `freshness.ok=false`（实测 2026-09-22 19:38）。
      即"函数没抛异常"被当成了"数据进来了"。
  (e) **表级失败** —— `db_update.tables[*].ok=false` 且 `rows=0`（实测 12/12）。

设计取舍(与本仓纪律一致)
------------------------
1. **只拦摄入, 不拦守护。** 门禁的"停手"对象是**数据摄入与依赖数据的下游动作**
   （选股/回测/训练）, 不是 daemon 自己 —— 把 daemon 也停了, 就再没有东西能发现
   数据恢复。这与 `kill_switch`「只停新开仓, 绝不停离场」是同一种"别把自己锁死"。
2. **判定异常不阻断, 但必须响亮。** 与本仓既有纪律一致（一个 bug 不能让系统静默
   停手）; 故门禁自身出错时返回 `allow=True` + 一条 `error`, 并落账本。
3. **连续失败才拦。** 单次抖动不该停手（网络瞬断很常见）。阈值见 `FAILS_TO_HALT`。
4. **留痕走哈希链。** 与 `kill_switch_ledger` 同理由: "谁在什么时候判定数据源死了"
   这一行若可被事后改写, 停手就无从追责。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

# `audit_chain` 与本模块同在 src/, 裸名导入只在 `src/` 进了 sys.path 时才成立 ——
# 直接 `python src/datasource_gate.py` 可以, 但 `python -m src.datasource_gate` 不行
# (包内名为 `src.audit_chain`, 裸名解析不到)。2026-09-22 实际踩到: 走 -m 时
# `record()` 每次都 `ModuleNotFoundError`, 被宽 except 吞掉后**静默退化成无哈希链的
# 平文件台账** —— 一个防篡改账本悄悄变成可随意改写的文本。这里显式补齐路径,
# 与 explain.py / signal_freeze_watch.py / health_state.py 同一做法。
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import audit_chain as _AC  # noqa: E402  (必须在 sys.path 补齐之后)

_TS_FMT = "%Y-%m-%d %H:%M:%S"

#: 数据源名称（稳定引用, 供测试与面板）
SRC_ENGINE = "stockdb_engine"      # 厂商行情引擎(SDK 直连, 127.0.0.1:7899)
SRC_DB_UPDATE = "db_update"        # 日更的多表同步
SRC_VIEWS = "factor_views"         # 因子视图物化

SOURCES = (SRC_ENGINE, SRC_DB_UPDATE, SRC_VIEWS)

#: `run_daily` 起跑时, **只有引擎探针**是当场新探的(账本里还没有)。
#:
#: 它喂进来的 `sync_step` / `db_update_step` 取自上**一轮**的 `daily_summary.json`
#: (刻意按 `basename(d) < day` 过滤掉当天), 故那两项**早已被上一轮记进账本**,
#: 不能算作"未入账观测" —— 否则同一次失败会被数两遍。
#:
#: 2026-09-23 实测后果: `db_update` 账本只有 2 条失败, 却报 `[HALT×3] allow=False`,
#: 会导致次日跳过摄入与选股(提前一天停手)。取个**有名字的常量**而不是在调用处
#: 手写 `("stockdb_engine",)`: 源名写错不会报错, 只会安静地把语义搞反 ——
#: 这正是本仓反复出现的"字符串魔法值"通病。
UNRECORDED_AT_DAILY_START = (SRC_ENGINE,)

#: **核心数据源** —— 死了才配 HALT(停止摄入与选股)。
#:
#: [2026-09-23 新增] 由用户提出的问题倒逼出来:
#: 「12 张辅助表全失败, 是否应该熔断整个交易管道?」
#:
#: ## 为什么 `db_update` **不**在这份名单里
#:
#: 三条实测证据(2026-09-23):
#:   1. `db_update` 的 12 张目标表**全部**经 DuckDB 写入, 而
#:      `data/legacy_stockdb.duckdb` **已退役删除**(`_connect_write_retry` 直接抛
#:      `FileNotFoundError`)。即: 它失败是**配置现实**, 不是数据源故障。
#:   2. 这 12 张表**无一**被决策链路引用 —— 全仓检索 `selector` / `factor_*` /
#:      `realtime_engine` / `paper_book` / `risk_*` / `pretrade_*` 对这些表名**零命中**
#:      (唯一命中在 `explain.py`, 而且是局部变量 `"events"` 的字面巧合)。
#:   3. 行情主链路走的是 **厂商引擎** `engine_bars_sync` -> h5i, 与 `db_update` 无关;
#:      实测引擎已追平(`engine_day=20260922 = expected`, `lag=0`)。
#:
#: ## 由此得出的判据(用户建议, 采纳)
#:
#: 「辅助数据坏了」与「交易数据坏了」必须**后果不同**: 前者 WARNING, 后者 HALT。
#: 用『辅助表失败』熔断『整个交易管道』是本末倒置 —— 它与本仓一贯禁止的
#: **静默降级**方向相反, 属**过度反应**: 不重要的事坏了, 导致重要的事停摆。
#:
#: ## 这份名单当前**只含引擎**, 刻意不含 `db_update`
#:
#: `stockdb_engine` 在两个含义上都是核心:
#:   · **是交易数据本身** —— 它的日线就是选股与撮合的输入;
#:   · **不是退役路径** —— 它走厂商 SDK 直连, 与 DuckDB 无关, 失败就是真故障。
#: 它落后超限 / 不可达时熔断是**正确**的(那时确实没有可信行情)。
#:
#: 而 `db_update` 的 12 张表**无一**被决策链路引用(见上), 且失败源于已退役的
#: 写入路径 —— 它够不上核心。
#:
#: > **将来若把某张 `db_update` 表接进决策链路, 必须同时把它加到这里。**
#: > 加表而不加这里, 就等于"它坏了系统也不停" —— 那才是真正的静默降级。
CRITICAL_SOURCES: tuple = (SRC_ENGINE,)

#: 判定档位
OK, DEGRADED, HALT = "OK", "DEGRADED", "HALT"
UNKNOWN = "UNKNOWN"

#: 连续失败多少轮才 HALT。
#: 依据本仓节奏: 数据源相关检查一轮 = daemon 的 5 分钟看护 / run_daily 的一次运行。
#: 取 3 而不是 1 —— 单次网络抖动很常见, 一次失败就停手会制造"自己把自己停掉"的
#: 假故障（本仓对"静默停手"零容忍, 对"误停手"同样零容忍）。
#: 取 3 而不是更大 —— 按 5 分钟轮询, 3 轮 = 15 分钟, 在 A 股 T+1 的日频策略里
#: 15 分钟来不及造成有意义的损失, 但足够确认"不是抖动"。
FAILS_TO_HALT = 3

#: 厂商发布宽限: 引擎落后**几个交易日以内**不告警。
#: 依据实测: 厂商在**盘后**才发布当日数据（2026-09-21 20:10 实测 engine_day 仍
#: 是 09-18; 2026-09-22 19:38 仍如此）。故"落后 1 个交易日"在盘后是**正常形态**,
#: 拦它会造成每天必然误报。超过宽限才说明厂商**漏发**（实测 2026-09-05..09-18
#: 断供 8 个交易日, 登记册 P1-DATA-STALE）。
PUBLISHER_GRACE_TRADING_DAYS = 1

#: 引擎落后**多少交易日**算硬故障（漏发/停摆）。取 3 —— 与 P1-DATA-STALE 实测的
#: 断供量级（8 个交易日）区分明确, 同时不至于把"长周末+1 天发布延迟"误判成故障。
ENGINE_LAG_HALT_TRADING_DAYS = 3

#: 前一轮结论的记忆（进程内）。跨进程的结论落在账本里。
_LAST: dict = {}


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ledger_path(path: str | None = None) -> str:
    return path or os.path.join(_repo_root(), "data", "datasource_health.jsonl")


def record(source: str, ok: bool, *, detail: str = "", kind: str = "",
           extra: dict | None = None, ledger: str | None = None, now=None) -> dict:
    """记录一次数据源探活结果(append-only, 走 #6 哈希链)。**失败不抛**。

    kind 用于区分失效模式(a)-(e), 使"连续失败"能按**同一原因**计数:
    交替出现的不同原因不该被当成"同一个故障持续"（那会误判为抖动而永不停手）。
    """
    now = now or datetime.now()
    rec = {"at": now.strftime(_TS_FMT), "source": str(source),
           "ok": bool(ok), "kind": str(kind or ""),
           "detail": str(detail or "")[:400]}
    if extra:
        rec["extra"] = extra
    fp = ledger_path(ledger)
    try:
        _AC.append(fp, rec, now=now)
        out = {"ok": True, "chained": True}
    except Exception as e:  # noqa: BLE001
        # 兜底写入: 先保住记录(留痕的**内容**比形式重要), 但绝不假装链是好的。
        # 历史教训(2026-09-22 实盘): 这里原来是裸 `except Exception` 且不记录原因,
        # 结果链退化成平文件**没有任何人知道** —— 探针的 GBK 解码异常每次都让链抛,
        # 4 条记录全是无 hash 的裸 JSON, 而 `chained: False` 被调用方直接丢弃。
        # 一条"防篡改"的审计链, 静默变成可随意改写的文本文件 —— 这正是最坏的一类失效。
        chain_err = f"{type(e).__name__}: {e}"
        try:
            d = os.path.dirname(fp)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(fp, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out = {"ok": True, "chained": False, "chain_error": chain_err}
        except Exception as e2:  # noqa: BLE001
            out = {"ok": False, "chained": False, "chain_error": chain_err,
                   "error": f"{type(e2).__name__}: {e2}"}
    out["record"] = rec
    return out


def read_ledger(ledger: str | None = None) -> list[dict]:
    fp = ledger_path(ledger)
    out: list[dict] = []
    try:
        with open(fp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return out
    return out


def consecutive_failures(source: str, *, ledger: str | None = None) -> dict:
    """该数据源**最近一段连续失败**的统计(到最近一次成功为止)。

    返回 {'n','kinds','same_kind','first_at','last_at'}。
    `same_kind` 为 True 表示这一串失败是**同一个原因** —— 只有它才够格触发停手
    （交替原因通常意味着探测本身不稳定, 该修探测而不是停数据）。
    """
    rows = [r for r in read_ledger(ledger) if str(r.get("source")) == str(source)]
    n = 0
    kinds: list[str] = []
    first_at = last_at = None
    for r in reversed(rows):
        if r.get("ok"):
            break
        n += 1
        last_at = last_at or r.get("at")
        first_at = r.get("at")
        k = str(r.get("kind") or "")
        if k and k not in kinds:
            kinds.append(k)
    return {"n": n, "kinds": kinds, "same_kind": len(kinds) <= 1,
            "first_at": first_at, "last_at": last_at}


# --------------------------------------------------------------------------
# 规则: 从**已有产物**判定五种失效模式
# --------------------------------------------------------------------------
def classify_engine(probe: dict | None, *, today_lag_hint: int | None = None) -> dict:
    """从 `engine_bars_sync --probe` 的输出判定引擎侧失效模式。

    `probe` 形如 {'ok','day','trading_days','error','freshness':{'ok','engine_day',
    'expected_day','lag_trading_days','error'}}。
    """
    p = probe or {}
    fresh = p.get("freshness") or {}
    lag = fresh.get("lag_trading_days")
    if lag is None:
        lag = today_lag_hint
    if not p.get("ok"):
        return {"ok": False, "kind": "unreachable",
                "detail": f"引擎不可达/无数据: {str(p.get('error') or '未知')[:160]}"}
    # [2026-09-22 P0] **日历强度必须先于落后天数被检查**。
    #
    # 为什么顺序不能反: `expected_day` 由 `trading_calendar` 的三层回退链给出。
    # 当它降级到「daily_bars 交叉」层时, "最后一个已收盘交易日"会退化成
    # "最后一个有数据的日子" —— 于是 `engine_day` 与 `expected_day` **同时**停在
    # 同一天, **落后恒为 0**, 报告上是一个漂亮的 0, 而「供应商未发布」这个哨兵
    # **已经失效了**。这与引擎把"无数据"说成"非交易日"是同一族错误的镜像:
    # **这里是把"日历降级"说成"刚好追平"。**
    #
    # 故: 非官方日历时一律判 `freshness_undeterminable`(**不可判定 != 健康**),
    # 并把来源带进 detail 供人排查 —— 与本模块既有的
    # "缺字段 = 无信息, 不据此拒单" 同一条纪律。
    strength = fresh.get("calendar_strength")
    if strength is not None and strength != "official":
        return {"ok": False, "kind": "freshness_undeterminable",
                "detail": (f"交易日历判据已降级为 {strength!r}"
                           f"(source={fresh.get('calendar_source')!r}) —— "
                           f"该层日历**停在数据停的地方**, 用它算出的落后天数恒偏小, "
                           f"故不可判定; 需先修日历缓存(data/trade_calendar.json 的 "
                           f"`days` 键)")}
    if lag is None:
        return {"ok": False, "kind": "freshness_undeterminable",
                "detail": "引擎可连但无法判定数据新鲜度(缺 freshness) —— "
                          "不可判定不等于健康"}
    try:
        lag_i = int(lag)
    except Exception:  # noqa: BLE001
        return {"ok": False, "kind": "freshness_undeterminable",
                "detail": f"lag_trading_days 非整数: {lag!r}"}
    if lag_i > ENGINE_LAG_HALT_TRADING_DAYS:
        return {"ok": False, "kind": "engine_lag_halt",
                "detail": (f"引擎落后 {lag_i} 个交易日 > 硬阈值 "
                           f"{ENGINE_LAG_HALT_TRADING_DAYS} "
                           f"(engine_day={fresh.get('engine_day')}, "
                           f"expected={fresh.get('expected_day')}) —— 疑漏发/停摆")}
    if lag_i > PUBLISHER_GRACE_TRADING_DAYS:
        return {"ok": False, "kind": "engine_lag_over_grace",
                "detail": (f"引擎落后 {lag_i} 个交易日 > 发布宽限 "
                           f"{PUBLISHER_GRACE_TRADING_DAYS} —— 疑厂商漏发")}
    if lag_i > 0:
        # 落后在宽限内: **正常形态**(厂商盘后才发布), 观察但不拦
        return {"ok": True, "kind": "publisher_grace", "observed_only": True,
                "detail": (f"引擎落后 {lag_i} 个交易日(在发布宽限 "
                           f"{PUBLISHER_GRACE_TRADING_DAYS} 内) —— 厂商盘后发布, "
                           f"属正常形态, 只观察不停手")}
    return {"ok": True, "kind": "", "detail": "引擎已追平最后已收盘交易日"}


def probe_engine(timeout: int = 120) -> dict:
    """真跑一次 `engine_bars_sync.py --probe`, 拿引擎可达性与新鲜度。

    抽成共享函数而非在 CLI 里内联, 因为**生产路径也需要它**: `run_daily` 的门禁
    放在摄入**之前**, 那一刻还没有当天的 `engine_bars_sync` 产物可看, 唯一能问
    "引擎现在到底在不在、数据到哪天" 的办法就是当场探一次。两处内联必然漂移,
    而漂移的后果是"一边以为查过了、另一边其实没查"。

    **两个必须显式写死的点(都踩过)**:
    1. `encoding="utf-8", errors="replace"`。中文 Windows 下 `text=True` 按 GBK 解码,
       而探针输出含中文; 错发生在**读取线程**里, `subprocess.run` **不抛**,
       而是静默返回 `stdout=None`。实测后果: 探针结果永远解析不到、`engine_probe`
       恒为 None, 于是"引擎落后/不可达"这个失效模式**从来没被真正检查过**。
    2. `if engine_probe is not None` 才检查引擎 ⇒ **传 None 等于放弃检查**。
       故任何非 None 的返回都必须是"查过且有结论", 查不成要返回 ok=False,
       而不是 None。本函数因此**任何异常都不抛**, 一律转成 ok=False 带原因。

    返回 {'ok','error','probe'}; `probe` 为原始探针 dict(成功时)。
    """
    import subprocess
    import sys as _sys
    try:
        out = subprocess.run([_sys.executable,
                              os.path.join(_repo_root(), "src", "engine_bars_sync.py"),
                              "--probe"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"探针执行失败: {type(e).__name__}: {e}"[:300]}
    txt = out.stdout
    if txt is None:
        return {"ok": False,
                "error": ("探针 stdout 为 None(解码线程失败); stderr="
                          f"{(out.stderr or '')[-160:]}")}
    i = txt.find("{")
    if i < 0:
        return {"ok": False, "error": f"探针无 JSON 输出: {txt[-160:]!r}"}
    try:
        p = json.loads(txt[i:])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"探针 JSON 解析失败: {type(e).__name__}: {e}"}
    if not isinstance(p, dict):
        return {"ok": False, "error": f"探针输出不是 dict: {type(p).__name__}"}
    return {"ok": True, "error": "", "probe": p}


def _d8(x) -> str:
    """把日期统一成 `YYYYMMDD` 再比较。

    **为什么必须归一**: `engine_bars_sync` 的 `engine_last_day` 是 `20260918`
    （8 位紧凑格式, 来自厂商 SDK）, 而 `h5i_max_before/after` 是 `2026-09-18`
    （带横线）。直接 `!=` 会把**同一天**判成不一致, 于是每一轮都报 `silent_no_op`
    假阳性 —— 首次实测就是这么报的。判据里任何跨来源的日期比较都要先归一。
    """
    s = str(x or "").strip()
    if not s or s.lower() in ("none", "nat"):
        return ""
    return s.replace("-", "").replace("/", "")[:8]


def classify_sync(step: dict | None) -> dict:
    """从 `run_daily.steps.engine_bars_sync` 判定失效模式 (d): 假成功。

    **核心判据**: `ok=true` 但 `appended=0` **且** 引擎确实领先 h5i(有该追的没追)
    —— 这才是"同步没干活"。若引擎与 h5i 同一天, `appended=0` 是正确行为, 不告警
    （否则每天都会误报"零行"）。

    注意与"厂商尚未发布"的区别: 引擎落后于**日历**是 `classify_engine` 的职责
    （那里有发布宽限）; 这里只管"引擎与 h5i 是否一致"。两者分开, 才能各自给出
    可行动的结论 —— 当天实测: 引擎 20260918 == h5i 2026-09-18, 即**同步本身是
    对的**, 问题在上游厂商没发布 09-21/09-22。把它报成"同步假成功"是归因错误。
    """
    s = step or {}
    if not s:
        return {"ok": False, "kind": "step_missing", "detail": "未执行该步骤"}
    if not s.get("ok"):
        return {"ok": False, "kind": "sync_failed",
                "detail": f"同步失败: {str(s.get('error') or s.get('note') or '')[:200]}"}
    appended = s.get("appended")
    need = int(s.get("missing_trading_days") or 0)
    planned = s.get("planned_days") or []
    engine_day = _d8(s.get("engine_last_day"))
    h5i_after = _d8(s.get("h5i_max_after") or s.get("h5i_max_before"))
    # "该追的没追"= 有明确计划/缺日, 或引擎**领先于** h5i(归一化后比较)
    behind = bool(need) or bool(planned) or bool(
        engine_day and h5i_after and engine_day > h5i_after)
    if appended == 0 and behind:
        return {"ok": False, "kind": "silent_no_op",
                "detail": (f"**假成功**: ok=true 但 appended=0, 而引擎数据"
                           f"({s.get('engine_last_day')}) 领先 h5i"
                           f"({s.get('h5i_max_after') or s.get('h5i_max_before')}) "
                           f"—— 该追的没追, 却被记成成功")}
    return {"ok": True, "kind": "",
            "detail": (f"appended={appended}; 引擎={s.get('engine_last_day')} "
                       f"h5i={s.get('h5i_max_after') or s.get('h5i_max_before')} "
                       f"(一致 => 无该追未追)")}


def classify_db_update(step: dict | None) -> dict:
    """从 `run_daily.steps.db_update` 判定失效模式 (c)/(e): 表级全失败。

    只看**表级**结果, 不看步骤级 `ok` —— 当天实测步骤级 `ok=false` 但**没有 error
    字段**, 而真正的信息在 `tables[*]` 里。故这里必须下钻。
    """
    s = step or {}
    if not s:
        return {"ok": False, "kind": "step_missing", "detail": "未执行该步骤"}
    tables = s.get("tables") or {}
    if not tables:
        return {"ok": False if not s.get("ok") else True,
                "kind": "no_table_detail" if not s.get("ok") else "",
                "detail": ("步骤失败但没有表级明细 —— 无法归因" if not s.get("ok")
                           else "无表级明细但步骤成功")}
    bad = [k for k, v in tables.items()
           if isinstance(v, dict) and not v.get("ok")]
    zero = [k for k, v in tables.items()
            if isinstance(v, dict) and (v.get("rows") or 0) == 0]
    n = len(tables)
    if len(bad) == n:
        # [2026-09-23] **先从表级 error 归因**, 再退回步骤级。
        # 为什么必须下钻到表级: `run_daily` 构造 `tables_dict` 时只留
        # `{"ok","rows"}`, **把每张表的 `error` 丢掉了**; 而步骤级 error 在
        # 这条路径上本来就是空的 —— 于是真因在回执里彻底消失, 只剩一句
        # 与事实相反的供应商自评(`akshare_stats: ok/0 错误/0 空`)。
        # 实测: 12/12 全失败的真因是 `DuckDB 已退役/不存在`, 与 akshare 无关。
        errs = sorted({str(v.get("error") or "").strip()
                       for v in tables.values()
                       if isinstance(v, dict) and str(v.get("error") or "").strip()})
        _eff = str(s.get("error") or "").strip() or (errs[0] if errs else "")
        # 「DuckDB 已退役」不是数据源故障, 是**配置现实**: 写入路径已迁 h5i,
        # 该文件被刻意删除。把它算成失败会让门禁每 3 天误熔断一次整个交易管道
        # (2026-09-23 实测正是如此)。故降级为**只观察**: 保留可见性, 不参与计数。
        if "已退役" in _eff or "legacy_stockdb" in _eff or "DuckDB 已退役" in _eff:
            return {"ok": True, "kind": "legacy_duckdb_retired", "observed_only": True,
                    "detail": (f"{n} 张表的写入路径走 DuckDB, 而 legacy DuckDB "
                               f"**已退役删除**({_eff[:90]}) —— 属已知配置现实, "
                               f"不参与连续失败计数; 行情主链路走 engine_bars_sync -> h5i")}
        return {"ok": False, "kind": "all_tables_failed",
                "detail": (f"**{n}/{n} 张表全部失败**且 rows=0"
                           + (f"; 步骤级未给原因" if not s.get("error") else f"; error={str(s.get('error'))[:120]}")
                           + (f"; 表级原因={_eff[:150]}" if _eff else "")
                           + f"; 供应商自评={json.dumps(s.get('akshare_stats'), ensure_ascii=False)[:100]}")}
    if bad:
        return {"ok": False, "kind": "partial_tables_failed",
                "detail": f"{len(bad)}/{n} 张表失败: {bad[:8]}"}
    if len(zero) == n:
        return {"ok": True, "kind": "all_zero_rows_observed_only",
                "observed_only": True,
                "detail": f"{n} 张表均 0 行(成功但无数据) —— 只观察"}
    return {"ok": True, "kind": "", "detail": f"{n} 张表, 0 行 {len(zero)} 张"}


def evaluate(*, engine_probe: dict | None = None, sync_step: dict | None = None,
             db_update_step: dict | None = None, ledger: str | None = None,
             unrecorded: frozenset | set | tuple = (), now=None) -> dict:
    """综合判定。**纯读 + 纯算**, 不改任何状态(记录由调用方决定是否 `record`)。

    返回 {'allow', 'level', 'items', 'halt_sources', 'reasons', 'checked_at',
          'thresholds'}

    `allow=False` 表示**应当停止数据摄入与依赖数据的下游动作**;
    **不表示停止守护进程**（见模块 docstring 的设计取舍 1）。

    ## `unrecorded` —— 这个参数的存在本身是一个 bug 的修复 (2026-09-23)

    连续失败的判据是"账本里最近一段连续失败有多长"。而下面这句
    `n_now = cf["n"] + 1` 假设**传进来的这次观测还没被记进账本**, 于是把它算作
    第 N+1 次。这个假设在**同一个观测被算两次**时就错了:

        run_daily 19:10 判一次 → `check_and_record` 把它记进账本
        → 19:15 守护进程读**当天** `daily_summary.json` 再判一次
        → `cf["n"]` 已经含它了, 却又 +1 ⇒ **同一次失败被数了两遍**

    2026-09-23 实测到的后果(真事故): 账本里 `db_update` 只有 **2** 条失败,
    而 `health_state` 报 **`[HALT×3]` ⇒ `allow=False`** ⇒ 次日(09-24)的
    `run_daily` 会**跳过摄入与选股**。不是"差一个数字"的问题, 是**提前一天停手**。

    故把那个隐含假设改成**显式入参**:
      · `unrecorded` 里的源 = "这次观测刚拿到、账本里还没有" ⇒ 计 `cf["n"] + 1`;
      · 其余源(默认全部) = "账本里已经有了" ⇒ 计 `cf["n"]`。
    默认 `()` 即"全部已记录" —— 只读型调用方(`health_state` 读当天已落盘产物)
    不必做任何事就得到正确语义。

    **为什么用"已记录源名"的显式列表, 而不是一个 bool**: bool 只能表达
    "全部未记录/全部已记录", 而真实调用是混合的 —— `run_daily` 的**引擎探针**
    是当场新探的(未记录), 它喂的 **sync/db_update 步骤**却是**上一轮**的产物
    (已记录)。用 bool 必然又要把其中一半算错。
    """
    now = now or datetime.now()
    items: list[dict] = []
    _unrec = {str(x) for x in (unrecorded or ())}
    _CRIT = {str(x) for x in CRITICAL_SOURCES}

    def add(source: str, cls: dict):
        items.append({"source": source, "ok": bool(cls.get("ok")),
                      "kind": cls.get("kind") or "",
                      "observed_only": bool(cls.get("observed_only")),
                      "detail": cls.get("detail") or ""})

    if engine_probe is not None:
        add(SRC_ENGINE, classify_engine(engine_probe))
    if sync_step is not None:
        # 同步步骤与引擎是**同一条链路**, 都归到引擎源下计数会有歧义;
        # 故它单独一个"源名", 但沿用同一套连续失败机制。
        add("engine_bars_sync", classify_sync(sync_step))
    if db_update_step is not None:
        add(SRC_DB_UPDATE, classify_db_update(db_update_step))

    halt_sources: list[str] = []
    degraded: list[str] = []
    reasons: list[str] = []
    #: [2026-09-23] 只观察(不参与 HALT 计数)的失败源 -> 归因文本。
    #: 与 `degraded` 分开记: 两者都会让 level=DEGRADED(可见), 但**都**不熔断 ——
    #: 若把辅助失败也计入 `halt_sources`, 就是用「辅助表坏了」停掉整个交易管道。
    auxiliary: dict[str, str] = {}
    for it in items:
        src = str(it["source"])
        if it["ok"]:
            if not it["observed_only"]:
                continue
            degraded.append(src)
            reasons.append(f"{src}: [观察] {it['detail']}")
            continue
        cf = consecutive_failures(src, ledger=ledger)
        # 账本里已有该源的失败次数; 若本次观测**尚未入账**, 才把它自己也算上。
        # (2026-09-23 修: 原实现无条件 +1, 使"同日再判一次"把同一次失败数两遍 ⇒ 提前 HALT)
        n_now = cf["n"] + (1 if src in _unrec else 0)
        same = cf["same_kind"] or not cf["kinds"]
        it["consecutive_failures"] = n_now
        it["same_kind"] = same
        it["counted_as_unrecorded"] = src in _unrec
        it["is_critical"] = src in _CRIT
        # [2026-09-23] **核心/辅助分流**: 只有核心源连续失败到阈值才熔断。
        # 辅助源(见 `CRITICAL_SOURCES` 的说明)仍如实记录、仍让 level=DEGRADED
        # (可见性不降级), 但**不**停掉交易管道 —— 用「辅助表坏了」熔断
        # 「整个交易管道」是过度反应, 与静默降级方向相反但同样错。
        if not it["is_critical"]:
            auxiliary[src] = it["detail"]
            degraded.append(src)
            reasons.append(f"{src}: [DEGRADED {n_now}/{FAILS_TO_HALT}, 辅助源不停手] {it['detail']}")
            continue
        if n_now >= FAILS_TO_HALT and same:
            halt_sources.append(src)
            reasons.append(f"{src}: [HALT×{n_now}] {it['detail']}")
        else:
            degraded.append(src)
            reasons.append(f"{src}: [DEGRADED {n_now}/{FAILS_TO_HALT}] {it['detail']}")

    level = HALT if halt_sources else (DEGRADED if degraded else OK)
    if not items:
        level = UNKNOWN
        reasons.append("没有任何数据源被判定 —— 门禁未生效(不等于健康)")
    return {
        "allow": not halt_sources,
        "level": level,
        "items": items,
        "halt_sources": halt_sources,
        "degraded_sources": degraded,
        "auxiliary_failures": auxiliary,
        "critical_sources": sorted(_CRIT),
        "reasons": reasons,
        "checked_at": now.strftime(_TS_FMT),
        "thresholds": {"fails_to_halt": FAILS_TO_HALT,
                       "publisher_grace_trading_days": PUBLISHER_GRACE_TRADING_DAYS,
                       "engine_lag_halt_trading_days": ENGINE_LAG_HALT_TRADING_DAYS},
        "scope": ("只拦数据摄入与依赖数据的下游动作(选股/回测/训练); "
                  "**不停守护进程** —— 停了就再没有东西能发现数据恢复"),
    }


def chain_health(ledger: str | None = None, *, tail: int = 200) -> dict:
    """账本自身的健康度: **链是否真的在链**。

    为什么需要它: `record()` 有兜底写入, 链断了记录照样落盘、判定照样返回 ——
    也就是说"链坏掉"这件事**不会以任何形式浮出水面**。一个防篡改账本退化成
    可随意改写的文本文件, 是最不该静默发生的事(2026-09-22 实际发生了一次:
    探针的 GBK 解码异常让链每次抛, 4 条记录全是无 hash 的裸 JSON)。
    这里主动去**验证链**, 把"退化"变成可观测数字: `unchained_tail` > 0 就是它在响。
    """
    fp = ledger_path(ledger)
    rows = read_ledger(fp)
    tail_rows = rows[-tail:] if tail and tail > 0 else rows
    unchained = sum(1 for r in tail_rows if not r.get("hash"))
    out = {"ledger": fp, "n": len(rows), "tail": len(tail_rows),
           "unchained_tail": unchained, "ok": unchained == 0 and len(rows) > 0}
    try:
        out["verify"] = _AC.verify(fp)
        out["ok"] = bool(out["verify"].get("ok")) and unchained == 0
    except Exception as e:  # noqa: BLE001
        out["verify_error"] = f"{type(e).__name__}: {e}"
        out["ok"] = False
    return out


def check_and_record(*, engine_probe: dict | None = None, sync_step: dict | None = None,
                     db_update_step: dict | None = None, ledger: str | None = None,
                     unrecorded: frozenset | set | tuple = (), now=None) -> dict:
    """判定 + 落账本（生产入口）。**判定异常返回 allow=True 并如实标注**。

    `unrecorded` 原样透传给 `evaluate` —— 见那里的完整说明。调用方必须诚实回答
    "我这次给的观测, 账本里到底有没有": 答错的后果不是数字不好看, 而是**提前停手**。
    """
    now = now or datetime.now()
    try:
        res = evaluate(engine_probe=engine_probe, sync_step=sync_step,
                       db_update_step=db_update_step, ledger=ledger,
                       unrecorded=unrecorded, now=now)
    except Exception as e:  # noqa: BLE001
        return {"allow": True, "level": UNKNOWN,
                "reasons": [f"门禁自身异常(不阻断, 需排查): {type(e).__name__}: {e}"],
                "error": f"{type(e).__name__}: {e}", "items": [],
                "halt_sources": [], "degraded_sources": []}
    writes: list[dict] = []
    for it in res["items"]:
        writes.append(record(it["source"], it["ok"], detail=it["detail"], kind=it["kind"],
                             extra={"level": res["level"],
                                    "consecutive_failures": it.get("consecutive_failures")},
                             ledger=ledger, now=now))
    if res["level"] != OK:
        writes.append(record("_gate", False, detail="; ".join(res["reasons"])[:400],
                             kind=res["level"], ledger=ledger, now=now))
    # 留痕是否**真的**留住了: 不能只看"记录写没写下去", 要看"链还是不是链"。
    failed = [w for w in writes if not w.get("ok")]
    unchained = [w for w in writes if w.get("ok") and not w.get("chained")]
    res["persist"] = {"n": len(writes), "written": len(writes) - len(failed),
                      "failed": len(failed), "unchained": len(unchained),
                      "chain_errors": [w.get("chain_error") for w in unchained][:3]}
    if failed or unchained:
        why = ("留痕写入失败: " + "; ".join(str(w.get("error")) for w in failed)[:200]
               if failed else
               "留痕**未走哈希链**(已落盘但可被改写): "
               + "; ".join(str(w.get("chain_error")) for w in unchained)[:200])
        res["level"] = (res["level"] if res["level"] != OK else UNKNOWN)
        res["reasons"] = list(res["reasons"]) + [f"账本退化(不阻断, 但必须修): {why}"]
        res["ledger_degraded"] = True
    return res


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="数据源健康门禁: 判定 + 查看账本")
    ap.add_argument("--evaluate", action="store_true",
                    help="跑一次真探针(engine_bars_sync --probe + 最近一次 daily_summary)并判定")
    ap.add_argument("--ledger", action="store_true", help="打印数据源健康账本尾部")
    ap.add_argument("--n", type=int, default=20, help="账本打印条数")
    args = ap.parse_args(argv)

    if args.ledger:
        rows = read_ledger()
        for r in rows[-args.n:]:
            print("  {at}  {source:20s} ok={ok!s:5s} kind={kind:24s} {detail}".format(
                at=r.get("at"), source=str(r.get("source")), ok=r.get("ok"),
                kind=str(r.get("kind"))[:24], detail=str(r.get("detail"))[:90]))
        print(f"  共 {len(rows)} 条")
        return 0

    # --evaluate: 取真探针（与 run_daily 用同一个 probe_engine, 避免两处实现漂移）
    _pr = probe_engine()
    engine_probe = _pr.get("probe") if _pr.get("ok") else {
        "ok": False, "error": _pr.get("error") or "探针失败"}
    sync_step, db_step = None, None
    try:
        p = os.path.join(_repo_root(), "data", "daily",
                         datetime.now().strftime("%Y%m%d"), "daily_summary.json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            sync_step = (d.get("steps") or {}).get("engine_bars_sync")
            db_step = (d.get("steps") or {}).get("db_update")
    except Exception:  # noqa: BLE001
        pass

    # 走 check_and_record 而非 evaluate: 手工跑一次探针也是**一次真实判定**,
    # 必须留痕 —— 否则"谁在什么时候判定数据源出问题"在手工排查时就断了链,
    # 而手工排查恰恰是最需要留痕的时候。连续失败计数也因此与自动跑批同源。
    res = check_and_record(engine_probe=engine_probe, sync_step=sync_step,
                           db_update_step=db_step)
    print(json.dumps({k: v for k, v in res.items() if k != "items"},
                     ensure_ascii=False, indent=2))
    print("--- items ---")
    for it in res["items"]:
        print("  {:20s} ok={!s:5s} kind={:24s} {}".format(
            it["source"], it["ok"], it["kind"][:24], str(it["detail"])[:110]))
    print("--- persist ---")
    print("  " + json.dumps(res.get("persist", {}), ensure_ascii=False))
    ch = chain_health()
    print("--- ledger chain ---")
    print("  n={n} unchained_tail={unchained_tail} ok={ok} verify={v}".format(
        n=ch.get("n"), unchained_tail=ch.get("unchained_tail"), ok=ch.get("ok"),
        v=(ch.get("verify") or {}).get("ok")))
    return 0 if res["allow"] else 2


if __name__ == "__main__":
    raise SystemExit(_main())
