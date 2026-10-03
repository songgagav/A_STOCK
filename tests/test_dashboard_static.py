# -*- coding: utf-8 -*-
"""dashboard 模板与静态资源的安全契约。"""
from __future__ import annotations

from pathlib import Path

import pytest

import dashboard


def test_dashboard_css_is_served_with_css_mime_and_etag():
    status, headers, body = dashboard._static_resource("/static/dashboard.css")

    assert status == 200
    assert headers["Content-Type"] == "text/css; charset=utf-8"
    assert headers["Cache-Control"] == "no-cache"
    assert headers["ETag"]
    assert body


def test_dashboard_js_is_served_with_javascript_mime():
    status, headers, body = dashboard._static_resource("/static/dashboard.js")

    assert status == 200
    assert headers["Content-Type"] == "application/javascript; charset=utf-8"
    assert body


def test_static_resource_outside_allowlist_is_forbidden():
    status, _headers, _body = dashboard._static_resource("/static/config.json")

    assert status == 403


def test_missing_allowlisted_static_resource_is_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "STATIC_ROOT", Path(tmp_path))

    status, _headers, _body = dashboard._static_resource("/static/dashboard.css")

    assert status == 404


@pytest.mark.parametrize(
    "request_path",
    [
        "/static/%2E%2E%2Fconfig",
        "/static/....//config",
        "/static/%2E%2E%5Cconfig",
    ],
)
def test_static_path_traversal_is_forbidden(request_path):
    status, _headers, _body = dashboard._static_resource(request_path)

    assert status == 403


def test_static_path_normalization_keeps_allowed_asset_accessible():
    status, _headers, body = dashboard._static_resource(
        "/static/subdir/../dashboard.css"
    )

    assert status == 200
    assert body


def test_missing_dashboard_template_fails_with_absolute_path(tmp_path):
    missing = Path(tmp_path) / "templates" / "dashboard.html"

    with pytest.raises(FileNotFoundError, match="Dashboard template not found"):
        dashboard._load_dashboard_page(missing)

    assert str(missing) in str(missing.resolve())


def test_dashboard_page_uses_external_resources_without_inline_css_or_js():
    assert "<style>" not in dashboard.PAGE
    assert "<script>" not in dashboard.PAGE
    assert '<link rel="stylesheet" href="/static/dashboard.css">' in dashboard.PAGE
    assert '<script src="/static/dashboard.js"></script>' in dashboard.PAGE
