# 端到端验证报告（三链路 × 时序 × 交叉 × 故障注入）

> **编排器**：`scripts/e2e_verify.py`　·　**报告产物**：`data/e2e_report.json`
> **本次执行**：`--phase all`（只读 + 故障注入）
> **总判定**：**No-Go**
>
> | 链路 | pass | fail | review | 判定 |
> |---|---|---|---|---|
> | 数据链 | 18 | 0 | 0 | **Conditional Go** |
> | 信号链 | 3 | 2 | 0 | **No-Go** |
> | 执行链 | 10 | 1 | 0 | **No-Go** |
>
> 判据（用户设计第五节）：未闭环 P0 → No-Go；有 P1 且有缓解 / 有需人工定性项 → Conditional Go；全通过 → Go。
>
> 演进：数据链 10/0/1 → 14/0/0 → **18/0/0**（写入原子性定性、第二批故障注入、备份恢复演练）；
> 执行链 4/1 → **10/1**（新增守护重启演练 6 项）。

---

## 〇、必须先纠正一处前提（否则后续工作建在错误归因上）

> 设计第三节写：「P0-2 的修复目标就是让第一条对比成立——当前偏差 1.02pp 正是因为两侧池口径不同源」。

**该归因已在本批次被证伪，请勿据此推进 P0-2。**

- 回放侧走的是 `backtest_engine._select_targets_hist()`，它严格执行候选目录 `C < D` 的**盘前视角**（该函数 docstring 记录了 v1/v2/v3 修复史）；
- 而"盘后档会污染池"指的是 `realtime_engine.load_targets()` 的第②档 `selection_same_day`——**回放并不调用它**。我曾据该假设给 `load_targets` 加 `skip_same_day_selection` 参数，**已回退**（改动前提不成立）。
- 实测佐证：当日 `selection.json` 确实存在且非空（0903/0904/0907/0908），所以"今日直接调 `load_targets` 会拿到不同池"成立——**但这是回放路径之外的事实**。

**结论**：1.02pp 的根因**仍未定**。候选方向见 `docs/preflight-verification.md` 三-1（成交/价格模型、调仓时点 `rebalance_interval_days=3`/`min_hold_days=2`、持仓状态初始化），需重新诊断。

---

## 一、三条主链路的入口/出口契约

| 主链路 | 入口 | 出口契约 | 编排器对应项 |
|---|---|---|---|
| 数据链 | 外部数据源 / h5i | h5i `daily_bars`/`valuation`/`financials` 行数与口径 | `preflight_data_checks`、`preflight_missing_data`、`preflight_atomicity` |
| 信号链 | 数据就绪 | 目标权重 + 门控状态（`regime`/`exposure_mult`/`freeze_new_buys`/`interval_days`） | `preflight_risk_triggers`、`preflight_circuit_breaker` |
| 执行链 | 订单计划 | 成交台账（`PaperBook`/`simulated_fills`）+ 对账记录 | `preflight_paperbook`、veighna `reconcile_astock.py` |

"上一环输出是否为下一环合法输入"的验证落点：
- 数据链→信号链：`preflight_data_checks` 的 **PIT 口径一致性**（5 份实现 / 102 期 → mismatch=0）+ 边界日 pb 覆盖；
- 信号链→执行链：目标权重契约（`preflight_risk_triggers` 的 target contract：n=10、missing_weight=0、`norm_sum=1.0`）；
- 执行链→账务：`preflight_paperbook.internal_recon`（自算权益 vs 快照权益差 1e-4；费用恒等差 0）。

---

## 二、盘前 → 盘中 → 盘后 时序验证（实测）

### 盘前

| 验证项 | 方法 | 通过标准 | 实测 |
|---|---|---|---|
| 数据就绪 | 哨兵查 pe_ttm/pb/float_shares 覆盖 | 无 CRITICAL | ✅ PIT 边界 4 个 pb 覆盖 0.9905~0.9934 |
| 口径正确 | 5 份 `_avail_date` 实现对比 102 期 | 无分歧 | ✅ `n_mismatch=0` |
| 并发读一致 | 单进程基线 vs 并发两进程行数 | 一致、无空表/锁冲突 | ✅ valuation 15416998 / financials 330983 三路一致 |
| 门控状态 | 构造 IC 序列触发 `risk`/`caution` | 与预期一致 | ❌ **7/10 断言通过**（详见下） |
| 健康检查 | `premarket_healthcheck.py` | 全绿才允许交易 | ⚠️ `FAIL=6`（3× arcticdb 缺包 + 3× h5i 数据真实滞后，见 `docs/vulnerability-register.md`） |
| 信号冻结（09:25 硬截止） | — | — | ⛔ **未实现/未验证**：代码中未见 09:25 信号冻结闸门 |

**门控 3 项未过（对应已登记 `P1-8`）**：
1. `IC 负 第3日(触发 risk)` → 实得 `caution`；`IC 恢复 第1/2日(仍 risk)` → 实得 `normal`
   —— 迟滞为 **5 日**（代码默认 3，`data/factor_gate_config.json`=5），导致**延迟进入、提前退出**；
2. **风险梯度不足**：`normal 1.0 / caution 0.95 / risk 0.9` —— 清单期望的"半仓"未出现，`risk` 仅降 10% 暴露；
3. **单日 −5% 与 −9% 响应完全相同**（均 `exposure_mult=0.9, freeze_new_buys=true, interval_days=6`）。
   `Sharpe 大跳变` 亦显示"risk 滞后进入：需连续 5 日确认"。

### 盘中

| 验证项 | 方法 | 通过标准 | 实测 |
|---|---|---|---|
| 下单带单号 | `preflight_paperbook` | 100% 带 `vt_orderid` | ✅ 模拟层；**真实通道 ⛔** |
| 风控触发 | `preflight_circuit_breaker` | 回撤 −8% 触发熔断级 | ❌ **FAIL**：`level=1`（仓位上限 0.7），未到 `level>=2` |
| 熔断升级 | 同上 | 回撤 −12% → `level=3` 清仓 | ✅ `level=3, limit=0.0` |
| CVaR / 波动拦截 | 同上 | 触发 | ✅ `cvar` / `volatility` 均触发 |
| 资金不足 | `preflight_paperbook` | 不越界、可复现 | ✅ |
| T+1 锁定 | 同上 | 买入当日记锁定 | ✅ |
| 幂等重放 | 同上 | 两次重放成交/现金/权益一致 | ✅ |
| 行情延迟 <5s | — | — | ⛔ **结构性不可验**（无实时行情源，tick 由 seed 构造） |
| 实时对账（每小时增量） | — | — | ⛔ **结构性不可验**（无真实回报流） |

### 盘后

| 验证项 | 方法 | 通过标准 | 实测 |
|---|---|---|---|
| 内部对账 | `preflight_paperbook.internal_recon` | 差≈0 | ✅ 权益差 1e-4、费用恒等 0 |
| 快照/恢复一致性（对应断线重连状态） | `snapshot_restore` | 逐位一致 | ❌ **FAIL**：`same_cash=False`（84979.35 vs 84979.35，实为分单位舍入 1.05e-4 元）→ 已登记 `P2-SNAPSHOT` |
| 逐笔对账（模拟盘） | veighna `reconcile_astock.py` | 5 项硬检查全过 | ✅ 70 笔带 `vt_orderid`，H1–H5 全 PASS（见 `docs/pit-valuation.md` §⑮） |
| 写入原子性（kill 中断） | `preflight_atomicity` | 完好旧态或完好新态 | ✅ **PASS（块粒度原子）**：基线 1000 → kill 后 8000，即 **7 个完整块、撕裂块 = 0**。每个已落盘的 `append` 块都是完整的 1000 行 ⇒ 库处于"若干完整块已提交"的一致状态。**提交粒度 = append 调用级（块），非逐行原子**，已写入脚本的自动判据（`pass` 布尔 + 块明细） |
| 数据缺失降级 | `preflight_missing_data` | 告警 + 降级，不静默 | ✅ 3/3（前向数据不足 → 合法 `[]`；篮子覆盖闸门生效；单标的空表 → 归属判定为合法空，兜底在批次层） |
| **备份恢复** | `preflight_backup_restore`（临时库/临时文件） | 恢复后逐行/逐字节一致 | ✅ **4/4**：B1 快照可建立枚举；**B2 变更 10 行 → `restore('t',1)` → 5 行且与快照前逐行一致**；B3 fork 隔离；B4 文件级"备份→损坏→还原"md5 一致（含中文） |
| 持仓/资金核对 vs 券商 | — | — | ⛔ **结构性不可验**（无券商侧真值） |

---

## 三、交叉验证：回测 vs 模拟盘（三角校验的第一条）

| 对比 | 通过标准 | 现状 |
|---|---|---|
| 回测 vs 模拟盘（同一决策日选股） | 逐位一致 | ❌ **未通过**：偏差 1.02pp（阈值 0.5pp）；**根因未定**（见第〇节：上版归因已证伪） |
| 回测 vs 实盘 | 逐位一致 | ⛔ 不可验（无实盘通道） |
| 模拟盘 vs 实盘 | 逐位一致 | ⛔ 不可验 |

工具：`src/layer3_parity.py`（回测 vs 回放）、`scripts/fidelity_compare.py`、`scripts/fidelity_rebalance.py`。

---

## 四、主动故障注入（第二阶段）

| 故障类型 | 注入方法 | 预期 | 实测 |
|---|---|---|---|
| 数据缺失 | `preflight_missing_data.py`（**真实条件**） | 告警 + 降级不静默 | ✅ 3/3 |
| 写入中断（等价"进程崩溃"） | `preflight_atomicity.py`（**真实 kill 写入进程**） | 完好旧态/新态 | ✅ 块粒度原子（7 完整块 / 撕裂 0） |
| 数据库故障（连接不可用） | 同上（kill 后只读校验） | 可读、不损坏 | ✅ kill 后可读 |
| **磁盘满** | `preflight_fault_inject.py`（**模拟 ENOSPC**） | 告警 + 可恢复 + 无残留 | ✅ 两轮注入均 FAIL；恢复后 OK；无残留探针文件 |
| **网络中断** | 同上（**模拟 URLError**） | 降级 + 重试 + 不静默 | ✅ `status=WARN degraded=True attempts=3`（实际调用 3 次）；恢复后可重跑 |
| **数据延迟（超时）** | 同上（**模拟 TimeoutError** 打 `dataguard.with_retry`） | 重试后告警、失败不缓存 | ✅ 重试 3 次后 `warn_once`；失败返回 `None` 不伪造数据；恢复后返回正常值 |
| 进程崩溃 + 守护重启 | `preflight_daemon_heal.py`（**真实 kill + 真实拉起**，临时端口） | 检测到崩溃→重建→留痕→幂等 | ✅ **6/6**（详见下方"演练抓到的真缺陷"） |
| 真实磁盘满 / 真实断网 | — | — | ⛔ 未演练（需小容量卷 / 可控网络隔离） |
| 部分成交 / 拒单 | `preflight_paperbook`（资金不足） | 告警，不重试死循环 | ✅ 模拟层；真实通道 ⛔ |

#### ⚠️ 演练抓到的真缺陷（已修）：`daemon._proc_alive` 会把已崩溃进程判为存活

**这是本次故障注入最有价值的产出** —— 它正是设计第四节"进程崩溃 → 守护重启"想抓的东西。

- **现象**：把"已崩溃的 pid"预置进 pidfile 后调用 `daemon._ensure_dashboard()`，看护**提前 return、根本没重建**；
  而演练脚本当时还把"新 pid 存活"当成通过 —— 那个"新 pid"其实就是**已死进程的 pid**，构成**假 PASS**（脚本 bug 也一并修了：现强制断言 **pid 必须更换**）。
- **根因**：`_proc_alive` 仅凭 `OpenProcess(PROCESS_QUERY_INFORMATION)` 成功判定存活；而 Windows 上**只要还有任何句柄**指向已终止的进程对象，`OpenProcess` 就会成功 —— 而 `subprocess.Popen` 在 `wait()` 之后**仍持有句柄**。
- **实测证据**（子进程 `exit(7)`、未释放其 Popen 句柄）：
  ```
  daemon._proc_alive(pid) = True     ← 错：把已退出判为存活
  GetExitCodeProcess      = False   ← 对
  ```
- **与既有注释的关系**：`daemon.py` 原注释称该缺陷已通过把权限位从 `PROCESS_TERMINATE(1)` 改为 `PROCESS_QUERY_INFORMATION(0x400)` 修好 —— **权限位解决不了句柄残留**，故修复不完整。
- **修法**：`_proc_alive` 增加退出码校验（仍在运行 ⇔ `STILL_ACTIVE(259)`）；拿不到退出码时保守沿用旧语义，避免把活进程误判为死亡而反复重建。
- **修后**：演练 **6/6 PASS**，且 pid 确实更换（旧 76760 → 新 33672）。
- **影响面**：`_ensure_dashboard` / `_ensure_obs_stack` / `_start_engine` 均以该判据决定是否重建，故此前**任一被看护进程若崩溃，看护可能不重建**。

> **第二批注入的性质声明（不得含糊）**：`preflight_fault_inject.py` 注入的是
> **模拟条件**（monkeypatch 出 `OSError(ENOSPC)` / `URLError` / `TimeoutError`），
> 打在**真实的代码路径**上（`check_disk` / `check_akshare` / `dataguard.with_retry`）。
> 它**不是**真的把磁盘写满、也**不是**真的断网 —— 不得据此宣称"已验证磁盘满/断网"。
>
> 用户要求「每次故障注入后必须验证**告警触发 + 系统恢复 + 数据一致**三件事」。
> 第二批 3 例**三件齐全**；第一批 `数据缺失` 三件齐全；`写入中断` 的"系统恢复"
> 由"kill 后库仍可读且块完整"体现（非进程重启恢复）。

---

## 五、结构性不可验证项（7 项，**不计入通过**）

| 链路/阶段 | 项 | 原因 |
|---|---|---|
| 执行链 | 真实下单成功率 | `TRADE_BROKER=paper`，无券商通道 |
| 执行链 | 真实拒单 / 部分成交 | 同上；paper 撮合的拒单语义不等价 |
| 执行链 | 券商回报对账（逐笔） | 本地 `vt_orderid` 由 paper 引擎生成，**≠ 券商回报** |
| 执行链 | 断线重连后状态一致性 | 无长连接可断 |
| 执行链 | 资金/持仓以券商为准核对 | 无券商侧真值 |
| 盘中 | 行情接收延迟 <5s | 无实时行情源（tick 由 seed 构造） |
| 盘中 | 实时对账（每小时增量） | 无真实回报流，只能事后对账 |

**范围声明**：本报告所有成交结论仅适用于**本地 `vnpy_paperaccount` 模拟撮合**；真实通道的下单/拒单/超时/断线/回报对账**未验证，不得外推**。

---

## 六、三阶段执行清单状态

```
第一阶段：只读验证（1天）
  [x] 盘前：数据就绪、口径、并发读、池构造      -> 通过
  [x] 盘前：门控状态                            -> 7/10（P1-8 三项未过）
  [ ] 盘前：信号冻结 09:25 硬截止                -> 代码中未见该闸门（全仓搜索零命中）
  [~] 盘中：行情延迟                            -> 结构性不可验
  [x] 盘中：下单带单号 / T+1 / 幂等 / 资金不足    -> 模拟层通过
  [x] 盘中：风控触发                            -> 回撤 -8% 未达熔断级（P0-3）
  [x] 盘后：内部对账                            -> 通过
  [ ] 盘后：快照/恢复逐位一致                    -> FAIL（P2-SNAPSHOT）
  [x] 交叉：回测 vs 模拟盘                      -> 已测，未通过（1.02pp，根因未定）

第二阶段：故障注入（1天）
  [x] 数据缺失                                  -> 3/3（三件事齐全）
  [x] 写入中断/库故障                            -> 块粒度原子（7 完整块 / 撕裂 0）
  [x] 磁盘满（模拟 ENOSPC）                      -> 告警+恢复+一致 三件齐全
  [x] 网络中断（模拟 URLError）                  -> 降级+重试+可恢复 三件齐全
  [x] 数据延迟（模拟 TimeoutError）              -> 重试后告警、失败不缓存 三件齐全
  [x] 进程崩溃 + 守护重启                        -> 6/6（真实 kill + 真实拉起；**并抓到 `_proc_alive` 真缺陷，已修**）
  [ ] 真实磁盘满 / 真实断网                       -> 未演练（需小容量卷 / 网络隔离）
  [x] 部分成交/拒单（资金不足）                   -> 模拟层通过

第三阶段：全链路 dry-run（1天）
  [~] 一个完整交易日，盘前 -> 盘中 -> 盘后        -> **已启动（QUANT_DATA_DIR 沙箱）；生产状态全程未被触碰**，但见下方观测
  [x] 监控告警验证                              -> ✅ **实测链路打通**：Prometheus 9090 规则组
                                                  `astock_db_stale` 的 `TableStale24h`=**FIRING** →
                                                  Alertmanager 9093 `TableStale24h` active →
                                                  alert_hook 9111 落 `logs/alerts.log`（153 行，
                                                  末条 19:18:57）。且该告警报的是**真问题**（h5i 滞后 11 天）
  [x] 备份恢复演练                              -> 4/4（临时库/临时文件，恢复后逐行/逐字节一致）
  [x] 输出 Go/No-Go 报告                        -> 本文件 + data/e2e_report.json
```

### dry-run 观测（`scripts/preflight_dryrun_day.py`，沙箱执行）

**安全结论（唯一关键的断言）**：运行全程对生产 `data/state.json` 与 `data/live_state.json`
做 md5 比对，**始终与备份一致** ⇒ `QUANT_DATA_DIR` 隔离有效，dry-run 未触碰实盘 paper 状态。

**观测到的实质问题**：本次以"今天"（2026-09-19）为消费日运行，而 h5i 数据只到 2026-09-08，
`load_targets()` 的前四档（当日 plan / 当日 selection / 跨日 plan / 跨日 selection）**都取不到**
⇒ 落进**第⑤档「现场选股」**。该档实测是**多核重计算**：进程 142 线程、累计 CPU 975s /
墙钟 3.6 min 仍未完成，临时目录尚无任何产物。

**为什么这条观测直接命中你关心的 09:25 缺口**：

- 设计的 09:25 硬截止要求"**信号在 09:25 前生成完毕且此后不变**"；
- 但只要当日池缺失、引擎回退到现场选股，**信号生成就会跨过 09:25 仍在进行**；
- 而 `P0-FREEZE-0925`（已登记）说明**没有任何机制阻止信号在 09:25 之后变更**。
- 两者叠加 ⇒ 「09:25 冻结」不是"加个时间判断"就够，还必须先解决**池缺失时的重计算耗时**
  （要么保证池必达，要么把现场选股移出盘中路径）。

> 说明：本次 dry-run 用的消费日是"今天"而非某个历史交易日，故它检验的是**降级路径**而非
> 正常路径。要检验正常路径需支持指定日期——`realtime_engine.py` 目前**没有 `--date` 参数**（实测），
> 这也是"完整交易日可复现 dry-run"当前的一个缺口。

### 边界说明：为什么"完整交易日 dry-run"我没有直接跑

`config.py:10` 是 `DATA_DIR = os.path.join(BASE, "data")` —— **没有环境变量覆盖**，因此无法把
数据目录指向沙箱。而 `realtime_engine.py --once` 会：

- 写 `data/live_state.json`（你正在看的 8000 面板每 3 秒轮询它）；
- **回写 `data/state.json`** —— 那是**实盘 paper 的持仓与净值**。

也就是说，跑这一次 dry-run 会**改动你的实盘模拟状态**。我已在**临时库/临时文件**上完成备份恢复
演练（4/4）作为前置条件，但**在未获你明确授权前不碰生产 state**。

要跑的话有两种方式，请你选：
1. **备份+还原方式**：我先把 `live_state.json` / `state.json` 备份，跑 `--once`，再逐字节还原并校验；（改动最小，但过程中你的实盘状态会被临时改写）
2. **加一个数据目录环境变量**（如 `ASTOCK_DATA_DIR`）让 `DATA_DIR` 可覆盖，然后我复制一份精简沙箱跑全流程；（更干净，但要改 `config.py`，属功能改动）

---

## 七、编排器实现要点（踩过的坑，供后续维护）

1. **这 6 个 preflight 脚本一律 `exit 0`，即使内部有 FAIL** ⇒ **退出码不可作判据**，必须解析各自 JSON。
2. **各 JSON 的 schema 互不相同**（`pass` 字典 / `assert_pass`+`total` / `_summary` / 纯 `verdict` 字符串）⇒ 一脚本一适配器，不能用通用提取器。
3. **必须校验 JSON 新鲜度**（`mtime > 本次脚本启动时刻`）：编排器初版曾登记一个**不存在的脚本**（`exit=127`），却因目录里残留同名 JSON 而读出 `pass` —— "脚本没跑成功却读出通过"。现已用 `_load(..., since=_RUN_START)` 堵住。
4. `REVIEW` **不计入 `fail`**：它表示"判据不足以自动定性"，与"实测失败"不同；误计入会把数据链从 Conditional Go 错判为 No-Go。

用法：
```powershell
$env:BAR_STORE = 'h5i'
& <持有 h5i_db 的解释器>\python.exe scripts\e2e_verify.py --phase readonly   # 第一阶段
& <持有 h5i_db 的解释器>\python.exe scripts\e2e_verify.py --phase all        # 加故障注入
```
