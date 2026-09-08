# -*- coding: utf-8 -*-
"""alert_hook.py -- Alertmanager 本地 webhook 通知接收端 (:9111/hook).

- 将 Alertmanager 推送的告警落盘到 logs/alerts.log (时间/名称/级别/标签/描述)
- 若环境变量 DING_WEBHOOK_URL 已设置(钉钉群机器人 webhook), 同时转发钉钉消息

用法: python ops/alert_hook.py [--port 9111]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(_BASE, "logs", "alerts.log")

DING_URL = os.environ.get("DING_WEBHOOK_URL", "")


def _log_line(line: str) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _forward_ding(msgs: list[str]) -> None:
    if not DING_URL:
        return
    try:
        import requests
        requests.post(DING_URL, json={
            "msgtype": "text",
            "text": {"content": "\n".join(msgs)[:1800]},
        }, timeout=6)
    except Exception as e:  # noqa: BLE001
        _log_line("[alert_hook] 钉钉转发失败: %s" % str(e)[:160])


class Hook(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默默认日志
        pass

    def _handle(self):
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(ln) if ln else b"{}"
            data = json.loads(body or b"{}")
        except Exception as e:  # noqa: BLE001
            data = {}
            _log_line("[alert_hook] 解析失败 %s" % str(e)[:120])
        msgs = []
        for al in data.get("alerts", []):
            lab = al.get("labels") or {}
            ann = al.get("annotations") or {}
            status = al.get("status", "?")
            name = lab.get("alertname", "?")
            sev = lab.get("severity", "-")
            summary = ann.get("summary") or ann.get("description") or ""
            line = "[%s] %s alertname=%s severity=%s status=%s | %s" % (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), status, name, sev,
                json.dumps(lab, ensure_ascii=False)[:160], summary)
            _log_line(line)
            msgs.append("%s: %s (%s) %s" % (status, name, sev, summary))
        if msgs:
            _forward_ding(msgs)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_POST = _handle
    do_GET = _handle


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9111)
    a = ap.parse_args()
    print("alert hook on :%d -> %s" % (a.port, LOG_PATH), flush=True)
    if DING_URL:
        print("dingtalk forward enabled", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Hook).serve_forever()
