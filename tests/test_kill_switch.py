# -*- coding: utf-8 -*-
"""三层 Kill Switch 的回归测试（路线图 #1）.

重点锁三件事:
  1. 三层各自独立生效, 且**可叠加**(一层出事不得掩盖另一层);
  2. **失效方向**: 文件不存在=不拉闸(否则首部署即静默停手); 文件损坏=**fail-closed**
     (忽略可能存在的紧急总闸, 后果远大于少做一天模拟盘);
  3. **留痕**: 拉闸/解除都进 append-only 账本, 谁/何时/因何。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from kill_switch import (  # noqa: E402
    engage, evaluate, read_global, record, release, verdict,
)

NOW = datetime(2026, 9, 21, 9, 20, 0)


class TestEvaluate:
    def test_no_signal_is_not_blocked(self):
        """三层都没信息 => 不拦(缺信息 ≠ 已拉闸)。"""
        assert evaluate(None, account={"state": "NORMAL"}, strategy={})["blocked"] is False

    def test_none_accounts_are_not_blocked(self):
        assert evaluate(None)["blocked"] is False

    def test_global_engage_blocks_with_actor_and_reason(self):
        v = evaluate({"engaged": True, "reason": "09-04 类数据污染待查",
                      "actor": "ops", "since": "2026-09-21 09:00:00"})
        assert v["blocked"] is True
        assert v["layers"] == ["GLOBAL"]
        assert "09-04 类数据污染待查" in v["reasons"][0]
        assert "ops" in v["reasons"][0]

    def test_global_engaged_without_reason_still_blocks(self):
        v = evaluate({"engaged": True})
        assert v["blocked"] is True
        assert "未注明原因" in v["reasons"][0]

    def test_global_engaged_false_is_not_blocked(self):
        assert evaluate({"engaged": False})["blocked"] is False

    def test_account_drawdown_blocks(self):
        v = evaluate(None, account={"state": "DRAW_DOWN"})
        assert v["layers"] == ["ACCOUNT"]
        assert "DRAW_DOWN" in v["reasons"][0]

    def test_strategy_ic_freeze_blocks(self):
        v = evaluate(None, strategy={"ic_freeze": True})
        assert v["layers"] == ["STRATEGY"]
        assert "IC 门控" in v["reasons"][0]

    def test_strategy_feed_stale_blocks_with_attribution(self):
        """行情源冻住时按陈旧价开新仓是本闸门的核心场景之一。"""
        v = evaluate(None, strategy={"feed_stale": True})
        assert v["layers"] == ["STRATEGY"]
        assert "行情源冻住" in v["reasons"][0]

    def test_layers_accumulate(self):
        """三层同时命中必须**全部**报出 —— 否则一层会掩盖另一层的证据。"""
        v = evaluate({"engaged": True, "reason": "r"}, account={"state": "DRAW_DOWN"},
                     strategy={"ic_freeze": True, "feed_stale": True})
        assert v["layers"] == ["GLOBAL", "ACCOUNT", "STRATEGY", "STRATEGY"]
        assert len(v["reasons"]) == 4

    def test_unknown_fields_ignored(self):
        assert evaluate(None, account={"state": "NORMAL", "mystery": 1},
                        strategy={"foo": True})["blocked"] is False


class TestFailSafe:
    """失效方向: 这是本模块最容易做错、后果最大的一处。"""

    def test_unreadable_global_state_fails_closed(self):
        v = evaluate(None, global_unreadable=True)
        assert v["blocked"] is True
        assert v["fail_closed"] is True
        assert "fail-closed" in v["reasons"][0]

    def test_unreadable_state_does_not_hide_other_layers(self):
        v = evaluate(None, account={"state": "DRAW_DOWN"}, global_unreadable=True)
        assert set(v["layers"]) == {"GLOBAL", "ACCOUNT"}

    def test_missing_file_is_not_blocked(self, tmp_path):
        g = read_global(str(tmp_path / "absent.json"))
        assert g == {"state": None, "unreadable": False}
        assert evaluate(g["state"], global_unreadable=g["unreadable"])["blocked"] is False

    def test_corrupt_file_is_unreadable(self, tmp_path):
        fp = tmp_path / "bad.json"
        fp.write_text("{半截", encoding="utf-8")
        g = read_global(str(fp))
        assert g["unreadable"] is True

    def test_non_dict_json_is_unreadable(self, tmp_path):
        fp = tmp_path / "arr.json"
        fp.write_text("[1,2,3]", encoding="utf-8")
        assert read_global(str(fp))["unreadable"] is True


class TestPersistenceAndAudit:
    def test_engage_release_roundtrip(self, tmp_path):
        st = str(tmp_path / "ks.json")
        lg = str(tmp_path / "led.jsonl")
        engage("数据污染待查", actor="ops", path=st, ledger=lg, now=NOW)
        v = verdict(path=st)
        assert v["blocked"] and v["layers"] == ["GLOBAL"]
        release(actor="ops", reason="已核实无污染", path=st, ledger=lg, now=NOW)
        assert verdict(path=st)["blocked"] is False

    def test_ledger_is_append_only_with_actor_and_time(self, tmp_path):
        st = str(tmp_path / "ks.json")
        lg = str(tmp_path / "led.jsonl")
        engage("r1", actor="ops", path=st, ledger=lg, now=NOW)
        release(actor="risk", reason="r2", path=st, ledger=lg, now=NOW)
        lines = [json.loads(x) for x in open(lg, encoding="utf-8").read().splitlines()]
        assert [x["action"] for x in lines] == ["engage", "release"]
        assert lines[0]["actor"] == "ops" and lines[1]["actor"] == "risk"
        assert lines[0]["ts"] == "2026-09-21 09:20:00"
        assert lines[1]["reason"] == "r2"

    def test_engage_over_corrupt_file_still_records(self, tmp_path):
        """文件损坏时人工拉闸仍应成功, 且账本留痕(不因读不出就拒绝人工动作)。"""
        st = tmp_path / "ks.json"
        st.write_text("{坏", encoding="utf-8")
        lg = str(tmp_path / "led.jsonl")
        engage("紧急", actor="ops", path=str(st), ledger=lg, now=NOW)
        assert verdict(path=str(st))["blocked"] is True

    def test_ledger_write_failure_does_not_raise(self, tmp_path):
        """留痕失败绝不能拖垮交易路径。"""
        record("GLOBAL", "engage", "ops", "r", ledger=str(tmp_path / "no" / "dir" / "x.jsonl"))

    def test_default_paths_are_under_data(self):
        from kill_switch import ledger_path, state_path
        assert state_path().replace("\\", "/").endswith("data/kill_switch.json")
        assert ledger_path().replace("\\", "/").endswith("data/kill_switch_ledger.jsonl")
