# -*- coding: utf-8 -*-
"""哈希链审计的回归测试（路线图 #6）.

核心: 不但要"改一行能发现", 还要"**删一行/换序/截尾**能发现" —— 后者才是账本被动手脚的
常见形态, 而裸 JSONL 对它们完全无感。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from audit_chain import GENESIS, append, compute_hash, head_path, verify  # noqa: E402


def _mk(tmp_path, n=3, name="led.jsonl"):
    fp = str(tmp_path / name)
    for i in range(n):
        append(fp, {"action": f"a{i}", "actor": "ops", "reason": f"r{i}"})
    return fp


def _lines(fp):
    return open(fp, encoding="utf-8").read().splitlines()


def _write(fp, lines):
    open(fp, "w", encoding="utf-8").write("\n".join(lines) + "\n")


class TestChainBasics:
    def test_genesis_prev_and_sequence(self, tmp_path):
        fp = _mk(tmp_path, 1)
        rec = json.loads(_lines(fp)[0])
        assert rec["prev"] == GENESIS and rec["seq"] == 0
        assert rec["hash"] == compute_hash(rec)

    def test_links_chain(self, tmp_path):
        fp = _mk(tmp_path, 3)
        recs = [json.loads(x) for x in _lines(fp)]
        assert recs[1]["prev"] == recs[0]["hash"]
        assert recs[2]["prev"] == recs[1]["hash"]

    def test_verify_clean_ledger(self, tmp_path):
        r = verify(_mk(tmp_path, 3))
        assert r["ok"] is True and r["n"] == 3 and r["pre_chain"] == 0

    def test_missing_file_is_ok_empty(self, tmp_path):
        r = verify(str(tmp_path / "absent.jsonl"))
        assert r["ok"] is True and r["n"] == 0

    def test_append_failure_does_not_raise(self, tmp_path):
        append(str(tmp_path / "no" / "dir" / "x.jsonl"), {"action": "x"})


class TestTamperDetection:
    def test_modified_record_detected(self, tmp_path):
        """把金额改小这类最常见的手脚。"""
        fp = _mk(tmp_path, 3)
        ls = _lines(fp)
        rec = json.loads(ls[1])
        rec["reason"] = "被改过的理由"
        ls[1] = json.dumps(rec, ensure_ascii=False)
        _write(fp, ls)
        r = verify(fp, check_head=False)
        assert r["ok"] is False and r["broken_at"] == 2 and "被改过" in r["reason"]

    def test_deleted_middle_record_detected(self, tmp_path):
        """删掉中间一条 => prev 链接断裂(裸 JSONL 完全看不出来)。"""
        fp = _mk(tmp_path, 4)
        ls = _lines(fp)
        del ls[1]
        _write(fp, ls)
        r = verify(fp, check_head=False)
        assert r["ok"] is False and "断裂" in r["reason"]

    def test_reordered_records_detected(self, tmp_path):
        fp = _mk(tmp_path, 4)
        ls = _lines(fp)
        ls[1], ls[2] = ls[2], ls[1]
        _write(fp, ls)
        assert verify(fp, check_head=False)["ok"] is False

    def test_tail_truncation_detected_via_head(self, tmp_path):
        """截断尾部: 链内部自洽(最后一条仍合法), 只有链头快照能发现。"""
        fp = _mk(tmp_path, 4)
        ls = _lines(fp)
        _write(fp, ls[:2])
        assert verify(fp, check_head=False)["ok"] is True      # 单看链是自洽的
        r = verify(fp)                                          # 带上链头
        assert r["ok"] is False and r["head_ok"] is False and "截断" in r["reason"]

    def test_vacuous_rewrite_of_whole_chain_is_beyond_local_chain(self, tmp_path):
        """**如实标注能力边界**: 连链头一起重算的对手, 本地链挡不住 —— 需把链头外发做锚定。
        本测试把这个已知边界固定下来, 免得后人误以为它是"不可篡改"。"""
        fp = _mk(tmp_path, 3)
        os.remove(head_path(fp))
        for i in range(3):
            append(fp, {"action": f"b{i}"})
        assert verify(fp)["ok"] is True      # 重写后自洽 => 本地无法分辨


class TestLegacyCompatibility:
    def test_pre_chain_records_are_reported_not_failed(self, tmp_path):
        """已存在的历史记录没有 hash: **不追溯补算**(事后补哈希证明不了原始性),
        如实报为 pre_chain, 链从下一条开始。"""
        fp = tmp_path / "legacy.jsonl"
        fp.write_text(json.dumps({"action": "old", "actor": "x"}, ensure_ascii=False) + "\n",
                      encoding="utf-8")
        append(str(fp), {"action": "new"})
        r = verify(str(fp))
        assert r["ok"] is True and r["n"] == 2 and r["pre_chain"] == 1

    def test_missing_hash_after_chain_started_is_a_break(self, tmp_path):
        fp = _mk(tmp_path, 2)
        ls = _lines(fp)
        rec = json.loads(ls[1])
        rec.pop("hash")
        ls[1] = json.dumps(rec, ensure_ascii=False)
        _write(fp, ls)
        r = verify(fp, check_head=False)
        assert r["ok"] is False and "缺 hash" in r["reason"]

    def test_corrupt_line_detected(self, tmp_path):
        fp = _mk(tmp_path, 2)
        ls = _lines(fp)
        ls.append("{半截")
        _write(fp, ls)
        r = verify(fp, check_head=False)
        assert r["ok"] is False and "JSON" in r["reason"]
