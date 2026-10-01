# -*- coding: utf-8 -*-
"""验证健康快照中的运行时模块版本是否等于当前磁盘源码版本。

用于部署后的硬验收；只读，不重启服务、不修改快照。退出码 0 表示一致，1 表示
不一致/缺字段/快照陈旧，2 表示脚本自身异常。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

import health_state as H  # noqa: E402


def verify(snapshot: str | None = None, max_age_s: float = H.PUBLISH_MAX_AGE_S) -> dict:
    path = snapshot or os.path.join(_BASE, "data", "health", "state.json")
    pub = H.read_published(path, max_age_s=max_age_s)
    current = H.module_code_version()
    loaded = ((pub.get("observed") or {}).get("code_version") or {})
    errors: list[str] = []
    if not pub.get("available"):
        errors.append(str(pub.get("error") or "健康快照不可用"))
    elif not loaded:
        errors.append("健康快照缺少 observed.code_version（守护可能尚未重启）")
    else:
        if loaded.get("matches") is not True:
            errors.append("快照记录的运行时哈希与其发布时磁盘哈希不一致")
        if loaded.get("loaded_sha256") != current.get("disk_sha256"):
            errors.append("快照运行时哈希与当前磁盘源码哈希不一致")
        if pub.get("stale"):
            errors.append(f"健康快照已陈旧: age_s={pub.get('age_s')}")
    return {
        "ok": not errors,
        "snapshot": path,
        "snapshot_ts": pub.get("ts"),
        "snapshot_age_s": pub.get("age_s"),
        "runtime_loaded_at": loaded.get("loaded_at"),
        "runtime_sha256": loaded.get("loaded_sha256"),
        "snapshot_disk_sha256": loaded.get("disk_sha256"),
        "current_disk_sha256": current.get("disk_sha256"),
        "errors": errors,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="部署后运行时/磁盘代码版本一致性检查")
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--max-age-s", type=float, default=H.PUBLISH_MAX_AGE_S)
    args = ap.parse_args(argv)
    try:
        result = verify(args.snapshot, args.max_age_s)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
