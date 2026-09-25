"""一次性: 更正 P1-DRLENVMISSING-KNOWN-STRUCTURAL (2026-09-26)。

**为什么要更正**: 该条把 `DrlEnvMissing` 标成「已知结构性误报」, 但查证发现前提是错的
—— `daemon.py:35` 的 `PY = sys.executable`(可被 `TRAE_PYTHON` 覆盖) 是 metrics_server
与 run_daily 的**同一个**来源, 且 `P0-DRLDEP` 已于 2026-09-20 由 `.venv310` 修复。
⇒ 探针探的解释器就是训练用的解释器, 规则本应长期 inactive; 实测确实 inactive。

**一条标着"已知误报"的真告警比没有告警更坏**: 真故障来时值班人会照注释忽略它。

保留原文 (标注 SUPERSEDED) 而不是删掉 —— 错误本身是记录的一部分。
"""
from __future__ import annotations

import json
import os
import shutil

FP = os.path.join("ops", "acceptance_status.json")
BAK = os.path.join("ops", "_acceptance_status.json.bak_pre_drlenv")

d = json.load(open(FP, encoding="utf-8"))
items = d["items"]

CORRECTION_DETAIL = """

---

## ⚠️ 更正 (2026-09-26): **它不是误报, 本条的标题与结论都是错的**

**上面这段保留原样, 作为记录** —— 但它的**核心前提是错的**, 请以下面的更正为准。

### 错在哪个前提

原文断言: 「`probe_runtime()` 探的是 **metrics_server 自己的** `sys.executable`(生产 VM CPython 3.10,
有 h5i_db 无 torch), 而 DRL 训练实际跑在 **`TRAE_PY`** 上」 ⇒ 两个解释器不同。

**这个"两个解释器"是我想象的, 没有查证。** 实际:

| 环节 | 实际 | 依据 |
|---|---|---|
| 解释器的**唯一**来源 | `PY = sys.executable`; 若 `TRAE_PYTHON` 存在则 `PY = TRAE_PYTHON` | `src/daemon.py:35-40` |
| metrics_server | `[PY, .../src/metrics_server.py, --port, 9101]` | `src/daemon.py:567-570` |
| run_daily (含 DRL 训练) | 同样由该守护以该 `PY` 启动 | `src/daemon.py` `_run_daily` |
| 生产守护用哪个解释器 | **`.venv310`** | `scripts/start_daemon.ps1` L21/L47 默认 `310` |

⇒ **所有子进程天然同源**。而 `P0-DRLDEP` 已于 **2026-09-20 修复**(`.venv310` 四项齐备),
所以探针探的解释器**就是**训练用的解释器, 那里**根本不缺依赖**。

### 实测 (决定性)

| 观测 | 值 |
|---|---|
| `astock_drl_env_ok` | **1** |
| `DrlEnvMissing` 规则状态 | **inactive**(从未真正故障) |
| `data/daily/20260925/daily_summary.json` → `drl_degrade.probe` | `ok=true, missing=[]`, 四项 deps 全 `true` |
| 同上 → `drl_degrade` | `ok=true, level=0, level_name=L0_正常` |
| 同上 → `drl_train` | `ok=true`, PPO, 800 timesteps, 产出 `data/drl/20260925/model.zip` (254944 bytes) |
| `.venv310` 真实 import | `h5i_db 0.1.6` + `torch 2.14.0+cpu` + `gymnasium 1.3.0` + `stable_baselines3 2.9.0` (CPython 3.10.11) |

### 我为什么会信错 (比这个 bug 更值钱)

我当时的"证据"是**真的**: 09-24 的 `drl_train_heartbeat.json` 确实是
`{phase: done, ok: true, total_timesteps: 800}`。**但这条证据同时兼容两个互斥假设**:

- (a) 探针探错了解释器 —— 训练成功、探针失败, 两者并存;
- (b) 探针在对的解释器上 —— 训练成功、探针成功, 两者并存。

**我拿一条两种假设都能解释的证据, 当成了对 (a) 的确认。**
真正的判别观测是「`PY` 从哪来」(查启动点), 而不是「训练成不成功」——
后者与两个假设都一致, 因而**没有判别力**。

> **判据**: 一条证据若能同时支持两个互斥假设, 它就不构成对任一假设的确认。
> 必须去找那个**能把两者分开**的观测。

### 危害形态 (与既有的 6 种都不同, 是 ⑥ 的反向)

不是"降级过程无告警"(⑥), 而是「**告警在, 但被文字解释掉了**」:
一条真告警被自己仓库里的注释标注为"已知误报" ⇒ 将来 `TRAE_PY` 真缺依赖时,
值班人会**照着这段注释把它忽略掉**。**比没有告警更坏。**

### 已落地的更正

- `ops/alert_rules.yml`: `DrlEnvMissing` 的 `summary` 去掉「已知结构性误报」;
  `description` 重写为「**本规则应当长期 inactive**」+ 四条可执行核对步骤
  (查 `TRAE_PYTHON` → 用该解释器跑 `probe_runtime` → 交叉核对 heartbeat →
  按 ops 流程重启) + **显式提醒不要因历史误标而忽略它**;
- `expr` (`astock_drl_env_ok == 0`) 与 `severity` (`warning`) **均未改动** ——
  错的只是文字, 不是逻辑;
- Prometheus 热重载 HTTP 200, 18 条规则 `health=ok`, 实测 18 条全 ok;
- `tests/test_engine_lag_alerting.py`: 原类 `TestKnownStructuralFalsePositiveIsLabelled`
  替换为 `TestDrlEnvMissingIsATrueAlertNotAFalsePositive`(**断言方向反转**),
  含"不得再出现误报字样"与"必须写明当初错在哪个前提"两条;
  已做**反向验证**: 把旧的错误 summary 放回去, 新守卫确实变红(1 failed / 16 passed)。

### 遗留 (本次未改, 已记)

`src/drl_degrade.py:439-440` 的 `probe_runtime().note` 仍是旧文案
(「h5i_db 的原生扩展仅支持 CPython 3.10, 故它无法与只支持 3.14 的 torch 侧共存于同一解释器」)
—— 该说法**已被 `.venv310` 证伪**, 但它是静态字符串、不影响判据, 故未擅动。
"""

CORRECTION_EVIDENCE = (
    "; **【2026-09-26 更正: 本条结论错误, 已反转】** "
    "查证 src/daemon.py:35-40 (PY 单一来源, TRAE_PYTHON 可覆盖) + :567-570 "
    "(metrics_server 用同一个 PY) + scripts/start_daemon.ps1 L21/L47 (默认 .venv310) "
    "⇒ 探针与训练**同源**, 且 P0-DRLDEP 已于 2026-09-20 修复 ⇒ 规则本应长期 inactive。"
    "实测: astock_drl_env_ok=1; 规则 inactive; daily_summary 20260925 的 "
    "drl_degrade.probe.ok=true/missing=[]、level=0/L0_正常、drl_train.ok=true "
    "(PPO 800 timesteps, model.zip 254944 bytes); .venv310 真实 import 四项齐备 "
    "(h5i_db 0.1.6 / torch 2.14.0+cpu / gymnasium 1.3.0 / stable_baselines3 2.9.0)。"
    "ops/alert_rules.yml 已改 summary/description, expr 与 severity 未动; "
    "Prometheus 热重载 HTTP 200, 18 规则 health=ok。"
    "tests/test_engine_lag_alerting.py 原类替换为 "
    "TestDrlEnvMissingIsATrueAlertNotAFalsePositive(断言方向反转), 并做反向验证: "
    "放回旧 summary ⇒ 新守卫变红(1 failed/16 passed), 证明非空断言。"
)

CORRECTION_NEXT = (
    "**本条已关闭(结论反转), 不要再按「已知误报」处理它。** "
    "`DrlEnvMissing` 现在是一条**真告警**, 应长期 inactive; 若它 firing, "
    "按 ops/alert_rules.yml 该规则 description 的四步排查。"
    "遗留: src/drl_degrade.py:439-440 的 probe_runtime().note 旧文案"
    "(称 h5i_db 与 torch 无法共存) 已被 .venv310 证伪, 待改(静态字符串, 不影响判据)。"
)

n = 0
for it in items:
    if it.get("id") == "P1-DRLENVMISSING-KNOWN-STRUCTURAL":
        it["title"] = (
            "【已更正 2026-09-26】DrlEnvMissing **不是**误报: 探针与 DRL 训练"
            "**共用同一个解释器**(daemon.py 单一 PY = .venv310), 规则本应长期 inactive"
        )
        it["status"] = "fixed"
        it["detail"] = (it.get("detail") or "") + CORRECTION_DETAIL
        it["evidence"] = (it.get("evidence") or "") + CORRECTION_EVIDENCE
        it["next"] = CORRECTION_NEXT
        n += 1

assert n == 1, f"expected exactly 1 entry updated, got {n}"

d["generated_at"] = "2026-09-26"
if not os.path.exists(BAK):
    shutil.copy2(FP, BAK)

with open(FP, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
    fh.write("\n")

print("updated entry:", n)
print("total items :", len(items))
