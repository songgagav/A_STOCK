from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_py310_setup_installs_and_verifies_vnpy():
    setup = (ROOT / "scripts" / "setup_py310_drl_venv.ps1").read_text(encoding="utf-8")

    assert "vnpy==4.4.0" in setup
    assert "'vnpy'" in setup


def test_runtime_docs_do_not_claim_vnpy_is_missing_from_venv310():
    docs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "README.md", ROOT / "docs" / "disciplines.md")
    )

    assert ".venv310" in docs
    assert "vnpy" in docs
    assert ".venv310 | 无 `vnpy`" not in docs
