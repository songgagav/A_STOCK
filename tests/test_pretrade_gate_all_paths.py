# -*- coding: utf-8 -*-
"""`pretrade_compliance.gate()` **四类裁决全路径**回归锁（含 2026-09-22 事故的正面回归）。

事故背景（本文件存在的理由）
--------------------------
2026-09-22 虚拟盘当天成交 3 笔买入，`data/order_audit.jsonl` **完全不存在** ——
"订单级细粒度流水"对**最常见的成功单**是一片空白，排查时无法回答"今天到底下过哪些单"。
根因：`gate()` 有四类裁决，其中三类写了审计，唯独
「**买入全部通过 -> execute**」这一条在 `return` 之前漏掉 `audit(...)`（模块 docstring
明写"正常：照常执行（留痕）"，实现独独漏了它）。已修：第 4 类现在也写审计。

同一事故还有第二处：引擎用 `getattr(self.pb, "equity", None)` 取权益，而 `PaperBook`
**没有 `equity` 属性**，结果恒为 `None`，使交易前清单的"单笔仓位上限"与"组合回撤"
两项永远只能记 `skip`；已改为 `RealtimeEngine._pb_equity()`。

本文件的纪律
-----------
· **只锁行为契约，不锁实现细节**：只断言"哪个 action / 哪句话出现在审计里"，
  不断言 hash 的具体值，也不断言审计里的文案逐字（只查关键子串），
  以免将来文案微调就把测试变成噪音。
· **绝不碰生产 `data/`**：所有裁决都传 `audit_fp`/`path` 指向 `tmp_path`；
  另有一个 autouse 哨兵（`_production_data_untouched`）在任何用例写坏生产流水时**报错**，
  `test_writes_never_touch_production_order_audit` 再正面清点一次（条数/mtime/md5）。
· **不修改仓库任何其它文件**：本文件是纯新增的测试，`src/` 不动。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import types
from datetime import datetime

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_REPO, "src") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO, "src"))

import pretrade_compliance as PC  # noqa: E402
import pretrade_gates as PG  # noqa: E402
from paper_book import PaperBook  # noqa: E402

#: 固定时钟 => 审计里的 ts 可断言，避免依赖运行时刻
NOW = datetime(2026, 9, 22, 9, 20, 0)

#: 与 tests/test_pretrade_compliance.py 同口径：slot = equity/max_pos = 20000
CTX = {"cash": 50000, "equity": 100000, "max_pos": 5, "min_cash": 5000,
       "tradable": True, "position_qty": 1000, "sellable_qty": 1000}

#: 审计格式契约：门禁放行所写记录必须自证的字段
AUDIT_FIELDS = ("ts", "action", "symbol", "side", "qty", "price", "actor", "reasons")

#: 四类裁决在本文件里各用哪一笔单触发（顺序即 gate 内部判定顺序）
CASES = {
    "reject": {"symbol": "600000", "side": "buy", "qty": 150, "price": 10.0},      # 非整手
    "sell_execute": {"symbol": "600000", "side": "sell", "qty": 500, "price": 10.0},  # 部分卖出
    "pending_approval": {"symbol": "600000", "side": "buy", "qty": 3000, "price": 10.0},  # 30000 > slot 20000
    "buy_execute": {"symbol": "600000", "side": "buy", "qty": 500, "price": 10.0},   # 正常补仓
}


# --------------------------------------------------------------------------- helpers

def _produced(order, ctx=None, *, tmp_path, name="a", **kw):
    """跑一次 `gate()`（审计/待批队列都指到 tmp_path）并返回 (裁决, 审计记录列表)。

    **路径隔离不是"顺手传一下"**：本函数自己断言 `audit_fp`/`path` 确实落在 tmp_path，
    传漏了就在测试里原地炸掉，而不是静默写生产。
    """
    a = str(tmp_path / f"{name}.jsonl")
    p = str(tmp_path / f"{name}_pending.json")
    for fp in (a, p):
        assert fp.startswith(str(tmp_path)), f"隔离路径必须落在 tmp_path: {fp}"
    res = PC.gate(dict(order), dict(CTX if ctx is None else ctx),
                  actor="engine", path=p, audit_fp=a, now=NOW, **kw)
    return res, _read(a)


def _read(fp):
    """读 JSONL 审计（不存在的文件 => 空列表，正是事故现场的那种"空白"）。"""
    if not os.path.isfile(fp):
        return []
    with open(fp, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f.read().splitlines() if ln.strip()]


def _assert_chain(recs):
    """哈希链契约：`seq` 递增、`prev` 环环相扣。**不断言 hash 的具体值**。

    这里刻意不比较 `hash == compute_hash(rec)` —— 那是在测 `audit_chain` 的算法
    （已有 `tests/test_audit_chain.py` 负责）；本文件只关心"经 audit 落盘的记录
    带没带链字段、链是不是接得上"。
    """
    for i, rec in enumerate(recs):
        for key in ("seq", "prev", "hash"):
            assert key in rec, f"第 {i + 1} 行缺链字段 {key}: {rec}"
        assert rec["seq"] == i
    assert recs[0]["prev"] == "0" * 64, "链首的 prev 必须是 genesis（全 0）"
    for i in range(1, len(recs)):
        assert recs[i]["prev"] == recs[i - 1]["hash"], f"第 {i + 1} 行 prev 链接断裂"


def _snap(fp):
    """(是否存在, 条数, mtime_ns, md5)；不存在时后三项为 None。"""
    if not os.path.isfile(fp):
        return (False, None, None, None)
    with open(fp, "rb") as f:
        blob = f.read()
    n = len([ln for ln in blob.decode("utf-8", "replace").splitlines() if ln.strip()])
    return (True, n, os.stat(fp).st_mtime_ns, hashlib.md5(blob).hexdigest())


@pytest.fixture(autouse=True)
def _production_data_untouched():
    """哨兵：任何用例把生产 `data/order_audit.jsonl` / `data/pending_orders.json`
    写出来或被改内容 => 直接报错。

    光靠"我记得传了 tmp_path"不是证据；这个夹具让**忘记传**变成红色失败。
    注意它只证明本文件新增的写入没落到生产：`gate()` 本身是纯函数式的，
    不传 `audit_fp` 才会写默认路径，而本文件每一次调用都传了。
    """
    a = PC.audit_path()
    p = PC.pending_path()
    before = (_snap(a), _snap(p))
    yield
    assert (_snap(a), _snap(p)) == before, (
        f"生产审计/待批文件在测试期间被改动 -> 有用例漏传 tmp_path: {a} / {p}")


def _fake_engine(pb):
    """真实 `RealtimeEngine._pb_equity` + 最小假账本（不跑引擎 `__init__`）。

    刻意用真实类而不是在测试里复写一份算法：让"引擎提供的 equity 到底是不是
    `cash + market_value()`"这件事由**被测实现**回答，而不是由测试自证。
    """
    import realtime_engine as RE
    eng = RE.RealtimeEngine.__new__(RE.RealtimeEngine)
    eng.pb = pb
    return eng


class _Pb:
    """最小账本替身：`cash` + `market_value()` + `snapshot()`。"""

    def __init__(self, cash=100000.0, mv=0.0, snap=None):
        self.cash = float(cash)
        self.market_value = lambda: float(mv)
        self.snapshot = (lambda: dict(snap)) if snap is not None else (lambda: {})


# ============================================================ 1) 四类裁决各自留痕

class TestGateFourDecisionsAudited:
    """四类裁决**各自**都要在订单流水里留痕 —— 漏任何一条都会让事后排查出现盲区。"""

    def test_normal_buy_is_audited(self, tmp_path):
        """【事故正面回归锁】正常放行的买入必须写审计。

        防的是 2026-09-22 那种失效：3 笔成功买入、占比最大的那类裁决，
        却因为 `return` 之前漏了 `audit(...)`，整份流水**从未被创建**。
        这条断言（放行 => 文件里有一条 execute）就是那次的替身。
        """
        r, recs = _produced(CASES["buy_execute"], tmp_path=tmp_path, name="ok_buy")
        assert r["decision"] == "execute"
        assert len(recs) == 1, "正常放行的买入必须留下恰好一条审计 —— 否则等于没有流水"
        rec = recs[0]
        assert rec["action"] == "execute"
        assert (rec["symbol"], rec["side"], rec["qty"], rec["price"]) == ("600000", "buy", 500, 10.0)

    def test_compliance_failure_is_audited_as_reject(self, tmp_path):
        """防"不合规的单悄悄消失"：拒单是对 agent 的反馈，规则问题必须可回查。"""
        r, recs = _produced(CASES["reject"], tmp_path=tmp_path, name="rej")
        assert r["decision"] == "reject"
        assert len(recs) == 1
        assert recs[0]["action"] == "reject"
        assert recs[0]["qty"] == 150
        assert any(PC.VIOL_LOT in str(x) or "非整手" in str(x) for x in recs[0]["reasons"])

    def test_sell_is_audited_as_execute_and_says_it_is_an_exit(self, tmp_path):
        """防"离场单没有留痕、事后看不出是止损还是事故"：
        卖出不走高危队列（设计取舍），所以它**只能**靠审计里的理由自证身份。"""
        r, recs = _produced(CASES["sell_execute"], tmp_path=tmp_path, name="sell")
        assert r["decision"] == "execute"
        assert len(recs) == 1
        assert recs[0]["action"] == "execute"
        assert recs[0]["side"] == "sell"
        assert any("离场" in str(x) for x in recs[0]["reasons"]), \
            f"审计里看不出这是离场单: {recs[0]['reasons']}"

    def test_high_risk_buy_is_audited_before_it_waits_for_a_human(self, tmp_path):
        """防"高危单入队了但账上看不出它被拦过"：
        3000 股 @10 = 30000 > 一个等权槽位 20000，转人工且**不执行**。"""
        r, recs = _produced(CASES["pending_approval"], tmp_path=tmp_path, name="pend")
        assert r["decision"] == "pending_approval"
        assert recs and recs[-1]["action"] == "pending_approval"
        assert any("槽位" in str(x) for x in recs[-1]["reasons"])

    def test_all_four_decisions_share_the_one_audit_ledger(self, tmp_path):
        """四类裁决写在同一条流水上（不是四个账本）—— 否则"今天下过哪些单"仍答不出来。"""
        seen = {}
        for key, order in CASES.items():
            res, recs = _produced(order, tmp_path=tmp_path, name=f"all4_{key}")
            assert recs, f"{key} 没有任何审计记录"
            seen[key] = (res["decision"], recs[-1]["action"])
        assert seen["reject"] == ("reject", "reject")
        assert seen["sell_execute"] == ("execute", "execute")
        assert seen["pending_approval"] == ("pending_approval", "pending_approval")
        assert seen["buy_execute"] == ("execute", "execute")


# ============================================================ 2) 审计格式契约

class TestAuditFormatContract:
    """订单流水的格式契约：**没有格式，"有流水"也读不出来**。"""

    def test_every_line_carries_the_contract_fields_and_links(self, tmp_path):
        """四类裁决的落盘记录都要含字段契约 + 哈希链（seq 递增 / prev 相接）。"""
        a = str(tmp_path / "fmt.jsonl")
        p = str(tmp_path / "fmt_pending.json")
        for key, order in CASES.items():
            PC.gate(dict(order), dict(CTX), actor="engine", path=p, audit_fp=a, now=NOW)
        recs = _read(a)
        # 每笔单**恰好**一条记录：高危单的"入队"与"裁决"是同一条（enqueue 内部落盘），
        # 人工批准/放行另有 `approved` / `approved_executed` 事件，不在本场景内。
        assert len(recs) == len(CASES), f"四类裁决应落 {len(CASES)} 条: {len(recs)}"
        for i, rec in enumerate(recs, start=1):
            for key in AUDIT_FIELDS:
                assert key in rec, f"第 {i} 行缺契约字段 {key}: {rec}"
            assert rec["ts"] == "2026-09-22 09:20:00"
            assert rec["actor"] == "engine"
            assert isinstance(rec["reasons"], list)
        _assert_chain(recs)
        actions = [r["action"] for r in recs]
        assert actions == ["reject", "execute", "pending_approval", "execute"], actions

    def test_normal_release_record_alone_also_satisfies_the_contract(self, tmp_path):
        """只跑"正常放行"这一条时，记录也必须自带链字段。

        事故当天流水文件不存在，所以"单条记录的格式"从未被任何真实数据检验过 ——
        这条把最小场景（只有一条 execute）单独钉住，避免它只在混合场景里偶然成立。
        """
        a = str(tmp_path / "one.jsonl")
        PC.gate(dict(CASES["buy_execute"]), dict(CTX), actor="engine",
                path=str(tmp_path / "one_pending.json"), audit_fp=a, now=NOW)
        recs = _read(a)
        assert len(recs) == 1
        for key in AUDIT_FIELDS:
            assert key in recs[0], f"缺契约字段 {key}: {recs[0]}"
        _assert_chain(recs)


# ============================================================ 3) 组合层清单接在同一咽喉点

class TestPortfolioChecklistOnTheSamePath:
    """[路线图 #15] 清单必须**接在 gate 这条咽喉点**上 —— 否则它只是个没人调的库函数。"""

    def test_checklist_pass_lets_the_buy_through(self, tmp_path, monkeypatch):
        """清单通过 => 正常放行；防"清单接反了（通过反而拒单）"这种接线级事故。"""
        monkeypatch.setattr(PG, "evaluate", lambda *a, **k: {"ok": True, "checks": [],
                                                            "failed": [], "reasons": []})
        r, recs = _produced(CASES["buy_execute"], tmp_path=tmp_path, name="gpass")
        assert r["decision"] == "execute"
        assert [x["action"] for x in recs] == ["execute"]

    @pytest.mark.parametrize("gates_ctx", [
        {"regime": "risk"},
        {"regime": "normal", "freeze_new_buys": True},
        {"regime": "risk", "freeze_new_buys": True},
    ])
    def test_blocked_regime_rejects_the_buy_with_a_checklist_reason(self, tmp_path, gates_ctx):
        """防"IC 门控已判 risk / 已冻结新仓，引擎却还在加仓"：
        买入必须被 reject，且理由里带清单项名与"交易前清单未通过"。

        `freeze_new_buys` 在本仓是**独立的布尔字段**（`factor_gate` 的返回值里
        `regime` 只取 normal/caution/risk，见 `apply_hysteresis`），故这里用它自己的
        键来构造"冻结新买入"，而不是把 "freeze_new_buys" 当成档位名。
        """
        ctx = {**CTX, **gates_ctx}
        r, recs = _produced(CASES["buy_execute"], ctx, tmp_path=tmp_path, name="grisk")
        assert r["decision"] == "reject", f"清单未通过却放行了: {r}"
        assert len(recs) == 1
        rec = recs[0]
        assert rec["action"] == "reject"
        joined = " ".join(str(x) for x in rec["reasons"])
        assert "交易前清单未通过" in joined and PG.CHK_IC_GATE in joined
        assert rec.get("pretrade_gates"), "被清单拒单时必须把逐项明细一并落审计"

    def test_literal_freeze_new_buys_regime_is_not_a_blocking_state(self, tmp_path):
        """锁住档位词表：`IC_BLOCKING_STATES` 只有 `risk`。

        把 "freeze_new_buys" 这个**字段名**当成档位名（`ctx["regime"]`）时不应拦单 ——
        它不是一个真实的 regime 取值（`factor_gate` 从不产出它），故"拦了"等于实现里
        混进了对字段名的误读。要拦新买入，唯一正确的入口是 `freeze_new_buys=True`
        （见上一条用例）。
        """
        assert "freeze_new_buys" not in PG.IC_BLOCKING_STATES
        r, recs = _produced(CASES["buy_execute"], {**CTX, "regime": "freeze_new_buys"},
                            tmp_path=tmp_path, name="gstr")
        assert r["decision"] == "execute"
        assert [x["action"] for x in recs] == ["execute"]

    @pytest.mark.parametrize("gates_ctx", [
        {"regime": "risk"},
        {"regime": "normal", "freeze_new_buys": True},
    ])
    def test_sell_is_not_blocked_by_the_checklist(self, tmp_path, gates_ctx):
        """防"把风险锁在仓里"：清单只管加仓，离场单在同一 ctx 下仍必须 execute
        （与 kill switch『只停新开仓, 绝不停离场』同一条纪律）。"""
        ctx = {**CTX, **gates_ctx}
        r, recs = _produced(CASES["sell_execute"], ctx, tmp_path=tmp_path, name="gsell")
        assert r["decision"] == "execute", f"离场单被清单拦下 = 把风险锁在仓里: {r}"
        assert recs and recs[-1]["action"] == "execute"


# ============================================================ 4) 判定异常不阻断交易

class TestChecklistCrashDoesNotBlockTrading:
    """清单自身出错 => **放行**但必须响亮留痕（一个 bug 不得让系统静默停手）。"""

    def test_crash_returns_execute_and_says_so_in_the_audit(self, tmp_path, monkeypatch):
        """清单抛异常 => 仍然 execute，但审计里必须出现"需排查"这句。

        关于**两条**记录（刻意写清，免得后人以为重复写是 bug）：异常分支先落一条
        `execute` + 该理由（这就是"响亮报出"），随后控制流继续走到正常放行路径，
        再落一条 `execute` + 空理由。两条都是必要的：前者是异常留痕，后者是放行留痕。
        故这里**不断言总数为 1**，只断言"有且只有一条带异常理由"，并锁住两者都在。
        """
        def _boom(*a, **k):
            raise RuntimeError("清单实现崩了")

        monkeypatch.setattr(PG, "evaluate", _boom)
        r, recs = _produced(CASES["buy_execute"], tmp_path=tmp_path, name="gboom")
        assert r["decision"] == "execute", "判定异常不得阻断交易（否则一个 bug 就让系统静默停手）"
        assert recs, "异常也必须留痕"
        assert {x["action"] for x in recs} == {"execute"}
        flagged = [" ".join(str(y) for y in x["reasons"]) for x in recs
                   if any("交易前清单执行异常" in str(y) for y in x["reasons"])]
        assert len(flagged) == 1, f"应恰好有一条带异常理由的记录: {flagged}"
        assert "交易前清单执行异常(不阻断, 需排查)" in flagged[0], flagged[0]
        assert "RuntimeError" in flagged[0], "异常类型要进审计，否则排查时只剩「放行了」这一句"


# ============================================================ 5) gates=False 只关清单、不关留痕

class TestGatesSwitchOnlyTurnsOffTheChecklist:
    """开关的**边界**：`gates=False` 关掉的是清单判定，不是留痕义务。"""

    def test_gates_false_skips_the_checklist_but_still_audits(self, tmp_path, monkeypatch):
        calls = []

        def _spy(*a, **k):
            calls.append(a)
            return {"ok": False, "checks": [], "failed": ["ic_gate_state"],
                    "reasons": ["不该被调用"]}

        monkeypatch.setattr(PG, "evaluate", _spy)
        r, recs = _produced(CASES["buy_execute"], tmp_path=tmp_path, name="goff", gates=False)
        assert calls == [], "gates=False 时不得调用清单（否则开关形同虚设）"
        assert r["decision"] == "execute"
        assert len(recs) == 1, "放行必须留痕，与清单开关**无关**（这是修复后的行为）"
        assert recs[0]["action"] == "execute"
        assert recs[0]["reasons"] == []


# ============================================================ 6) PaperBook 没有 equity 属性

class TestPaperBookHasNoEquityAttribute:
    """锁住"`PaperBook` 没有 `equity` 属性"这一**前提**，并锁住权益的正确算法。

    为什么这不是废话：如果有人"顺手"给 `PaperBook` 加一个语义不同的同名属性
    （比如"仅现金"或"仅市值"），`getattr(self.pb, "equity", None)` 会立刻从 `None`
    变成**一个看起来合理的错数**，清单会静默地用错的权益判仓位与回撤 ——
    比现在的"记 skip"更难发现。故这里正面断言它不存在。
    """

    def test_no_equity_attribute_exists(self):
        pb = PaperBook(init_capital=100000)
        assert hasattr(pb, "equity") is False, (
            "PaperBook 不得拥有 equity 属性：权益必须现算 cash + market_value()；"
            "该属性一旦存在，引擎 getattr 探到的将是一个语义易漂移的值")

    def test_snapshot_equity_equals_cash_plus_market_value(self):
        pb = PaperBook(init_capital=100000)
        snap = pb.snapshot()
        assert snap["equity"] == pb.cash + pb.market_value()

    def test_snapshot_equity_equals_cash_plus_market_value_with_positions(self):
        """有持仓时同样成立 —— 否则"权益"与"现金"会被混为一谈。"""
        pb = PaperBook(init_capital=100000)
        pb.positions["600000"] = {"qty": 1000, "avg_cost": 10.0,
                                  "buy_date": "2026-09-22", "locked_qty": 1000}
        pb.d_price["600000"] = 12.0
        snap = pb.snapshot()
        assert pb.market_value() == 12000.0
        assert snap["equity"] == pb.cash + pb.market_value() == 112000.0


# ============================================================ 7) _pb_equity() 单元测试

class TestPbEquityHelper:
    """`_pb_equity()` 是"清单第一项输入"的唯一来源；它取不到时必须说"不知道"。"""

    def test_uses_snapshot_equity_when_available(self):
        eng = _fake_engine(_Pb(cash=1.0, mv=1.0, snap={"equity": 123456.78}))
        assert eng._pb_equity() == pytest.approx(123456.78)

    def test_falls_back_to_cash_plus_market_value_when_snapshot_raises(self):
        class _BadSnap:
            cash = 100.0

            def market_value(self):
                return 23.0

            def snapshot(self):
                raise RuntimeError("snapshot 炸了")

        assert _fake_engine(_BadSnap())._pb_equity() == pytest.approx(123.0)

    @pytest.mark.parametrize("pb", [
        # snapshot 没有 equity（返回 {}）、现金与市值都为 0
        _Pb(cash=0.0, mv=0.0, snap={}),
        # snapshot 明确给出 0 => 不采信，退回现算，现算也是 0
        _Pb(cash=0.0, mv=0.0, snap={"equity": 0.0}),
        # snapshot 取不到 + 现算为 0
        _Pb(cash=0.0, mv=0.0),
        # snapshot 抛异常 + 现算为 0
        _Pb(cash=0.0, mv=0.0, snap=None),
    ])
    def test_returns_none_not_zero_when_equity_is_unknown(self, pb):
        """**取不到就返回 None，绝不能返回 0**：0 会被清单读成"权益为零"，
        让所有仓位占比变成 inf（一个假数比"不知道"有害得多）。"""
        eq = _fake_engine(pb)._pb_equity()
        assert eq is None, f"未知权益必须是 None（清单据此 skip），实际得到 {eq!r}"

    def test_zero_equity_is_unknown_not_zero(self):
        """账本现金与市值真的都是 0 时，也返回 None（0 不是"已知的零"）。"""
        assert _fake_engine(_Pb(cash=0.0, mv=0.0))._pb_equity() is None
        assert _fake_engine(_Pb(cash=0.0, mv=0.0))._pb_equity() != 0

    def test_snapshot_and_market_value_both_broken_returns_none(self):
        class _BothBroken:
            cash = None

            def market_value(self):
                raise RuntimeError("market_value 也炸了")

            def snapshot(self):
                raise RuntimeError("snapshot 炸了")

        assert _fake_engine(_BothBroken())._pb_equity() is None

    def test_engine_equity_feeds_the_checklist_position_item(self):
        """接线锁：引擎给的权益必须真的能让清单算出"单笔占权益 X%"（事故第二处的回归）。

        修复前 `getattr(pb, 'equity', None)` 恒为 None，清单第一项永远只能记
        "缺 qty/price/equity, 无法算仓位占比"。这里用**真实** `_pb_equity()`
        的输出喂给**真实** `PG.evaluate`，断言那一项不再是"缺字段"。
        """
        eng = _fake_engine(_Pb(cash=100000.0, mv=0.0, snap={"equity": 100000.0}))
        ctx = {"equity": eng._pb_equity(), "cash": 100000.0, "max_pos": 5,
               "position_qty": 0, "sellable_qty": 0, "regime": "normal"}
        g = PG.evaluate({"symbol": "600000", "side": "buy", "qty": 500, "price": 10.0}, ctx)
        pos = [c for c in g["checks"] if c["name"] == PG.CHK_POSITION][0]
        assert "缺 qty/price/equity" not in str(pos.get("detail")), pos
        assert pos.get("measured") is not None, pos


# ============================================================ 8) 生产 data/ 隔离证据

class TestNoProductionWrites:
    """本文件的每次裁决都必须落在 tmp_path —— 测试绝不许污染生产订单流水。"""

    def test_writes_never_touch_production_order_audit(self, tmp_path):
        """正面清点：跑完四类裁决后，生产 `data/order_audit.jsonl` 的
        存在性 / 条数 / mtime / md5 全部不变。

        注意本仓现状：该文件**本就不存在**（正是 2026-09-22 事故的现场）。
        所以"文件仍是 None"本身就是最强的证据 —— 若有用例漏传 `audit_fp`，
        这里会看到它被凭空创建出来。
        """
        prod = PC.audit_path().replace("\\", "/")
        assert prod.endswith("data/order_audit.jsonl"), prod
        before = _snap(prod)
        for key, order in CASES.items():
            _produced(order, tmp_path=tmp_path, name=f"prod_{key}")
        assert _snap(prod) == before, "生产订单流水被测试写入/改动了"

    def test_every_gate_call_in_this_module_is_path_isolated(self, tmp_path):
        """反面兜底：给 gate 传默认路径（`audit_fp=None`）时它**会**写生产文件。

        这条不是在鼓励那么做，而是证明"传 tmp_path"是真正起作用的隔离手段 ——
        否则上一条断言可能只是因为"根本没写文件"而假绿。
        用 monkeypatch 把默认路径搬到 tmp_path 后，再确认文件确实被创建。
        """
        real_audit_path = PC.audit_path
        moved = str(tmp_path / "moved_order_audit.jsonl")
        PC.audit_path = lambda path=None: moved if path is None else real_audit_path(path)
        try:
            a = str(tmp_path / "isolated.jsonl")
            PC.gate(dict(CASES["buy_execute"]), dict(CTX), actor="engine",
                    path=str(tmp_path / "isolated_pending.json"), audit_fp=a, now=NOW)
            assert os.path.isfile(a), "显式传 audit_fp 时必须写到该路径"
            assert not os.path.isfile(moved), "显式传 audit_fp 时不得落到默认路径"
        finally:
            PC.audit_path = real_audit_path
