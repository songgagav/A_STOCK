import os
import sys


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))


def test_console_json_is_ascii_safe_for_windows_code_pages():
    import run_daily

    rendered = run_daily.format_console_json({"action": "⇒"})

    assert "⇒" not in rendered
    assert "\\u21d2" in rendered
