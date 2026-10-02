# -*- coding: utf-8 -*-
"""P0-2 read-only diagnostic input-shape contracts."""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts", "research"))

import p0_2_diagnose as diag  # noqa: E402


def test_trade_symbols_reads_current_dict_trade_records():
    history = {"2026-09-02": [
        {"type": "buy", "canon": "600000.SH"},
        {"type": "sell", "symbol": "000001.SZ"},
    ]}
    assert diag.trade_symbols(history, ["2026-09-02"]) == [
        ("buy", "600000.SH"), ("sell", "000001.SZ")]


def test_trade_symbols_keeps_legacy_pair_records_compatible():
    history = {"2026-09-02": [("buy", "600000.SH")]}
    assert diag.trade_symbols(history, ["2026-09-02"]) == [("buy", "600000.SH")]
