# -*- coding: utf-8 -*-
"""Dashboard visual V1 的结构与设计令牌契约。"""
from __future__ import annotations

import re
from pathlib import Path

import dashboard


CSS = (Path(dashboard.STATIC_ROOT) / "dashboard.css").read_text(encoding="utf-8")
JS = (Path(dashboard.STATIC_ROOT) / "dashboard.js").read_text(encoding="utf-8")
HTML = dashboard.PAGE


def test_visual_tokens_pin_terminal_palette_and_typography():
    expected = {
        "--bg-0": "#060a0e",
        "--bg-1": "#0a1017",
        "--bg-2": "#0e1620",
        "--line-0": "#0f1822",
        "--line-1": "#1a2733",
        "--ready": "#8fe7ff",
        "--frozen": "#d89460",
        "--up": "#ff6b62",
        "--down": "#4dda94",
        "--warn": "#ffd06b",
        "--danger": "#ff5468",
    }
    for token, value in expected.items():
        assert re.search(rf"{re.escape(token)}\s*:\s*{re.escape(value)}", CSS)
    assert "--mono:" in CSS
    assert re.search(r"font-variant-numeric\s*:\s*tabular-nums lining-nums", CSS)
    assert re.search(r"--r\s*:\s*1px", CSS)


def test_visual_skeleton_exposes_ticker_session_and_freeze_tracks():
    for marker in (
        'id="ticker"',
        'id="sessionRail"',
        'id="freezeTrack"',
        'id="tickerEq"',
        'id="tickerPnl"',
        'id="tickerPct"',
        'id="tickerPos"',
        'id="tickerFreeze"',
        'id="tickerLate"',
        'id="tickerMode"',
        'id="tickerSession"',
    ):
        assert marker in HTML


def test_visual_status_updates_use_existing_freeze_api_without_new_endpoint():
    assert "/api/signal-freeze" in JS
    assert "function renderTicker" in JS
    assert "function updateSessionRail" in JS
    assert "/api/attribution" not in JS
    assert "/api/live/series" not in JS
