# -*- coding: utf-8 -*-
"""V3 frontend contract for the Hero daily-return strip."""
from __future__ import annotations

import os
from pathlib import Path


_REPO = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HTML = (_REPO / "templates" / "dashboard.html").read_text(encoding="utf-8")
_CSS = (_REPO / "static" / "dashboard.css").read_text(encoding="utf-8")
_JS = (_REPO / "static" / "dashboard.js").read_text(encoding="utf-8")


def test_hero_v3_keeps_cards_mount_and_adds_daily_series_mount():
    """旧 JS 挂载点保留，同时提供独立的走势挂载点。"""
    assert 'id="cards"' in _HTML
    assert 'id="heroSeries"' in _HTML


def test_hero_v3_consumes_daily_series_without_new_api():
    """Hero 从 /api/live 的 daily_series 渲染，不新增接口依赖。"""
    assert "function renderHeroSeries" in _JS
    assert "daily_series" in _JS
    assert "daily_return" in _JS
    assert "/api/live" in _JS


def test_hero_v3_has_honest_empty_state_and_return_colors():
    """没有历史时显示积累中，并沿用 A 股涨跌颜色。"""
    assert "历史数据积累中" in _JS
    assert ".hero-series" in _CSS
    assert ".hero-series__bar.is-up" in _CSS
    assert ".hero-series__bar.is-down" in _CSS
