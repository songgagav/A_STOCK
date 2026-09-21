# -*- coding: utf-8 -*-
# secret-scan: skip-file
#   本文件是扫描器的**测试夹具**: 必须包含伪造的密钥形态(sk-/ghp_/AKIA/PEM/内嵌凭据 URL)
#   才能做正向控制 —— 否则"扫描器能抓"这件事无法被证明。故文件级豁免。
#   代价: 本文件内部的真泄露不会被报。这正是豁免只能用于夹具文件的原因。
"""密钥扫描器的回归测试（SEC-2）.

**正向控制**证明"它真会抓"（否则 0 命中毫无意义）;
**负向控制**证明"它不乱喊"（把 `os.environ[...]` 也报出来的扫描器会被整体忽略）。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from secret_scan import _is_placeholder, scan_text  # noqa: E402


def _rules(text, name="x.py"):
    return sorted(h["rule"] for h in scan_text(text, name))


class TestPositiveControls:
    """正向: 真的泄露形态必须被抓住。"""

    def test_openai_style_key(self):
        assert "known_prefix" in _rules('k = "sk-abc123DEF456ghi789JKL0"')

    def test_github_token(self):
        assert "known_prefix" in _rules('t = "ghp_0123456789abcdefghijklmnopqrstuvwx"')

    def test_aws_key_id(self):
        assert "known_prefix" in _rules('aws = "AKIAIOSFODNN7EXAMPLE"')

    def test_pem_private_key(self):
        assert "known_prefix" in _rules("-----BEGIN RSA PRIVATE KEY-----")

    def test_hardcoded_secret_assignment(self):
        assert "hardcoded_secret" in _rules('TUSHARE_TOKEN = "9f3a1c7e5b2d8a40f6c1"')

    def test_env_file_tracked_is_critical(self):
        hits = scan_text("A=1\n", ".env")
        assert hits and hits[0]["rule"] == "env_tracked" and hits[0]["severity"] == "CRITICAL"

    def test_env_example_is_allowed(self):
        assert scan_text("A=1\n", ".env.example") == []

    def test_url_with_credentials(self):
        assert "url_with_creds" in _rules('DB = "postgres://root:hunter2xyz@db:5432/x"')

    def test_snippet_redacts_the_value(self):
        """命中的**片段本身不得回显密钥** —— 否则扫描输出又成了新的泄露面。"""
        hits = scan_text('API_KEY = "9f3a1c7e5b2d8a40f6c1abc"')
        assert hits and "9f3a1c7e5b2d8a40f6c1abc" not in hits[0]["snippet"]
        assert "<redacted>" in hits[0]["snippet"]


class TestNegativeControls:
    """负向: 正确写法与占位符不得报(否则等于没有这个检查)。"""

    @pytest.mark.parametrize("line", [
        'key = os.environ["OPENAI_API_KEY"]',
        'key = os.environ.get("TUSHARE_TOKEN", "")',
        'key = getenv("SIMNOW_PASSWORD")',
        'os.environ["API_KEY"] = load()',
        'KEY = ""',
        'KEY = None',
        'API_KEY = "your-key-here"',
        'TOKEN = "changeme"',
        'SECRET = "${MY_SECRET}"',
        'PASSWORD = "<从环境变量注入>"',
        'API_KEY = "example-token"',
        'TOKEN = "xxxxxxxxxx"',
        'password_field = "请输入密码"',
    ])
    def test_correct_forms_not_flagged(self, line):
        assert _rules(line) == [], f"误报: {line}"

    def test_env_example_with_placeholders_not_flagged(self):
        text = ('OPENAI_API_KEY=your-key-here\nTUSHARE_TOKEN=\n'
                'SIMNOW_PASSWORD=${SIMNOW_PASSWORD}\n')
        assert scan_text(text, ".env.example") == []

    def test_repeated_char_value_is_not_a_secret(self):
        assert _is_placeholder("aaaaaaaaaaaa") is True


class TestExemption:
    def test_line_level_exemption_requires_reason_marker(self):
        assert _rules('TUSHARE_TOKEN = "9f3a1c7e5b2d8a40f6c1"  # secret-scan: ok: 测试夹具') == []

    def test_exemption_is_per_line(self):
        text = ('TUSHARE_TOKEN = "9f3a1c7e5b2d8a40f6c1"  # secret-scan: ok: 夹具\n'
                'OTHER_TOKEN = "8e2b1d6f4c3a9b50e7d2"\n')
        hits = scan_text(text)
        assert len(hits) == 1 and hits[0]["line"] == 2


class TestRepoIsClean:
    def test_repository_has_no_tracked_secret(self):
        """本仓基线: 跟踪文件零命中。**若哪天不为零, 先看是不是真泄露再谈别的。**"""
        from secret_scan import scan_repo
        r = scan_repo(_REPO)
        assert r["findings"] == [], f"跟踪文件出现疑似密钥: {r['by_rule']}"

    def test_no_tracked_env_file(self):
        from secret_scan import tracked_files
        bad = [f for f in tracked_files(_REPO)
               if os.path.basename(f).startswith(".env") and not f.endswith(".example")]
        assert bad == [], f"不得跟踪真实 .env: {bad}"
