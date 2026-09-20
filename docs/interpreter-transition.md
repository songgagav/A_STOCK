# 生产解释器过渡：`.venv314` → `.venv310`（观察性过渡）

> 背景：`h5i_db`（主数据源）只支持 CPython 3.10，而 torch 侧此前只在 3.14 的 `.venv314`
> 里。`P0-DRLDEP` 即由此而来。2026-09-20 用 **(c) 路线**新建了 `.venv310`
> （Python 3.10.11 + h5i-db + torch/gymnasium/stable-baselines3 **同一解释器**），
> 详见 `scripts/setup_py310_drl_venv.ps1`。
>
> **但切换解释器不只是 DRL 路径变了 —— 所有 Python 代码的运行环境都变了。**
> 故按下述"观察性过渡"执行，`保留 .venv314` 作为回退。

## 为什么必须过渡而不是直接切

| 风险 | 说明 | 本次已有的缓解/证据 |
|---|---|---|
| 浮点/库版本差异 | 3.10 与 3.14 的 numpy/pandas 版本不同 | `preflight_interp_parity.py` 的 `FLOAT_TOL` 分类实测 **0 条** |
| 隐式依赖差异 | 3.14 下可用但 3.10 下缺的包 | 已发现并解决：`.venv314` 缺 `h5i_db`（见下"实测发现"） |
| 执行时间差异 | 解释器/库版本不同 | 实测**两次运行方向相反**，属冷启动/磁盘缓存噪声，**不据 n=2 下结论** |
| 日志/输出差异 | 异常栈、warning 格式可能不同 | 归一化后**逐行 0 差异**（见下） |

## 工具

```powershell
# 同输入 · 双解释器 dry-run 对比（自动判定"逐位一致 or 差异可解释"）
.venv310\Scripts\python.exe scripts\preflight_interp_parity.py `
    --a-py .venv310\Scripts\python.exe `
    --b-py .venv314\Scripts\python.exe `
    --date 2026-09-08 --json-out reports\interp_parity_20260908.json
```

它复用 `scripts/preflight_dryrun_day.py`（沙箱 DATA_DIR + 种入 daily/drl 输入 +
生产 `state.json`/`live_state.json` md5 前后比对），跑的正是
`realtime_engine.load_targets()`（**决策/档位**）与 `RealtimeEngine.run_tick()`（**信号/撮合**）。
每次运行给独立 `DRYRUN_DATA_DIR`，两个解释器互不干扰。

差异分四类，**只有 SEMANTIC 必须为 0**：

| 类别 | 含义 | 处置 |
|---|---|---|
| `SEMANTIC` | 真实语义差异 | **必须 0**，否则判 FAIL |
| `FLOAT_TOL` | 两侧浮点相对差 ≤ 1e-9 | 可解释（跨版本末位差异） |
| `TIMESTAMP` | **整串**为墙钟时间戳（按**值的形态**判，不靠键名白名单） | 可解释 |
| `VOLATILE` | 时间戳/耗时/临时路径等已知可变字段 | 可忽略，但计数并列出 |

## 实测结果（2026-09-20）

两个日期、**两个不同档位**各跑一次，均 **PASS**：

| 交易日 | 命中档位 | 决策一致 | SEMANTIC | FLOAT_TOL | 日志归一化差异 | 结论 |
|---|---|---|---|---|---|---|
| 2026-09-08 | `selection_same_day` | 8/8 | 0 | 0 | 0 行 | 通过 |
| 2026-09-05 | `drl_cross_day` | 8/8 | 0 | 0 | 3/2 行 | 通过（但有 WARN，见下） |

两次的受保护生产文件 `state.json` / `live_state.json` 均未变。
（`data/preflight_dryrun.json` 会被 dry-run 按设计改写，非受保护文件。）

### ⚠ 最重要的发现：输出相同，但**降级原因不同**

2026-09-05 两侧**都**落到 `used=fml_fallback`，所以结构化产物对比是 **0 差异**；但原因完全不同：

| 解释器 | 降级原因 |
|---|---|
| `.venv310` | `reason=fusion 覆盖不足: n_sc=7 req=10 cov=0.70 < 门槛 0.8` ← **数据问题（真实覆盖不足）** |
| `.venv314` | `reason=fusion 异常: ModuleNotFoundError: No module named 'h5i_db'` ← **环境问题（读不到主源）** |

**这正是"只看输出会以为没问题"的典型**：`.venv314` 下 fusion 每次都是因为**读不到主数据源**
而回退，而不是因为数据真的不够。故对比工具专门加了 **③b 降级原因对比**，
并在原因不同时打 `WARN`（`degrade_reason_mismatch: true`），而不是简单判 PASS 了事。

⇒ 这也从**独立角度**再次印证了 `P0-DRLDEP`：`.venv314` 下的流水线一直跑在降级模式。

## 过渡步骤（用户批准的三天路径）

### 第一天（本日已完成）
- [x] 用 `.venv310` 跑完整 dry-run（不接生产）
- [x] 与 `.venv314` 的 dry-run 输出对比（决策 / 信号 / 日志 / 降级原因）
- [x] 确认逐位一致或差异可解释 —— **两个日期的 SEMANTIC 均为 0**

### 第二天
- [ ] 把 daemon 切到 `.venv310`：`& .venv310\Scripts\python.exe src\daemon.py`
      （`daemon.py` 用 `PY = sys.executable`，故**必须用该解释器启动 daemon 本身**）
- [ ] **保留 `.venv314` 作为回退**
- [ ] 密切观察首日全链路：`data/daily/<day>/daily_summary.json` 的
      `steps.drl_train` / `steps.drl_degrade` / `steps.drl_post`，以及
      `data/drl_degrade_events.jsonl`、`data/drl_post_metrics.jsonl` 是否开始逐日产出

### 第三天及以后
- [ ] 稳定则正式切换
- [ ] 异常则**立即回退**：改回 `.venv314\Scripts\python.exe` 启动 daemon 即可
      （回退不需要改任何代码 —— 这正是决策 D 把"跑在错解释器上"做成
      **响亮 L3 + CRITICAL 告警**而非静默失效的价值）

## 回退路径（保留至少一周）

`.venv314` **保持原样不动**。回退 = 换启动命令，预计 5 分钟内完成。
判据（任一即回退）：

- `probe_runtime()` 不再返回 ok=True；
- `daily_summary.json` 出现新的 `degradation`/`spc_check` 异常，或 `drl_degrade` 级别非 0 且非预期；
- `metrics_server` 的 `astock_drl_env_ok` 变为 0；
- 盘前链路耗时出现数量级恶化（对比上面 dry-run 的耗时基线）。
