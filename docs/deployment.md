# 部署指南

完整生产运行时统一使用仓库根目录的 `.venv310`。它必须同时能够导入
`h5i_db`、`torch`、`stable_baselines3` 和 `vnpy`；`.venv314` 仅用于纯 Python
研究和兼容性回归，不通过 `PYTHONPATH` 混用两个环境的 site-packages。

## 1. 环境准备

- Python 3.10.11（完整运行时由 `scripts/setup_py310_drl_venv.ps1` 创建）
- Python 3.14 可作为纯 Python 研究/兼容性回归环境
- 轻量运行(选股/门控/回测/检测):numpy, pandas, scipy, scikit-learn, h5py, pyarrow
- DRL 链路(可选):torch, stable-baselines3, gymnasium
- 数据:需自行准备本地行情/财务/估值数据(推荐 free-stockdb / CNEquity 镜像)

```bash
pip install -r requirements_314.txt
python -m pytest tests/ -q        # 自检
```

## 2. 路径与数据源(环境变量)

仓库不硬编码个人路径;按需设置(复制 `.env.example` 参考):

| 变量 | 说明 |
|---|---|
| `STOCKDB_ROOT` | StockDB/free-stockdb 部署数据根(parquet 分片、`data`/`mydb`、`stockdb.conf`) |
| `STOCKDB_PYBAO_DIR` | 可选的明确 SDK 覆盖路径；默认使用项目内 `vendor/stockdb/pybao` |
| `STOCKDB_ENGINE` | StockDB 本地端点，默认 `127.0.0.1:7899` |
| `ARCTIC_URI` | ArcticDB LMDB,如 `lmdb://<path>/arcticdb` |
| `PYBAO_DIR` | 旧版外部 SDK 路径，仅作项目 bundle 缺失时的兼容回退 |
| `TRAE_PYTHON` | 守护进程使用的外部 Python(含依赖) |
| `FACTOR_HEALTH_ENABLED` | 因子健康隔离开关(默认 `1`;`0` 关闭) |
| `OVERFIT_RESULTS_FILE` | 过拟合检测窗口样本源(可选覆盖) |

> Windows 持久化示例:`setx STOCKDB_ROOT "E:\data\stockdb"`。**修改环境变量后需新开终端再启动守护**,否则新进程读不到。StockDB 的 SDK 和服务程序默认取项目内 `vendor/stockdb`，但数据工作目录仍必须是 `STOCKDB_ROOT`；不要把行情数据库目录复制进仓库。

## 3. 运行模式

```bash
# 一次性/日频（使用 .venv310）
.venv310\Scripts\python.exe src/run_daily.py # 收盘选股主流程
.venv310\Scripts\python.exe src/gate_refresh_daemon.py # IC 缓存刷新守护 (交易日 16:05-16:50 窗口)
# 估值补丁观察：当前公开 checkout 未包含 sentinel_daemon.py，按 docs/patch-retirement-watch.md 人工验收
.venv310\Scripts\python.exe src/daemon.py                 # 交易日守护
.venv310\Scripts\python.exe src/realtime_engine.py --once # 盘中撮合单次
.venv310\Scripts\python.exe src/dashboard.py --port 8000  # Web 面板

# 后台常驻(观测栈 + 上述守护) 一键拉起, 幂等, 重复执行不会起第二个
powershell -ExecutionPolicy Bypass -File ops/start_obs_stack.ps1
```

### 3.2 09:25 信号冻结 Shadow 运行

信号冻结的操作边界和观察记录见 [`docs/evolution/signal-freeze-runbook.md`](evolution/signal-freeze-runbook.md)。默认模式是 `shadow`：引擎可以生成并验证 09:25 快照，但不会因为观察功能改变现有消费路径。

交易日运行前必须确认：

1. `read_mode_control('.')` 返回 `mode=shadow`；
2. `TRADE_BROKER` 为 `paper` 或未设置（默认 `paper`）；
3. `data/trade_calendar.json` 存在且覆盖当前日期；
4. `daemon.py` 已启动并能在 09:25 运行引擎。

09:25 后只接受已验证的 `signal_snapshot_<YYYYMMDD>.json` 作为冻结证据。快照不存在、格式无效或哈希不匹配时，自动链路只估值、不调仓；不得人工补写快照来“恢复”当日自动调仓。午间重选写入迟到归档，必须经完整候选池/权重人工审核后，才可作为次日可选输入。

非交易日不生成观察日。2026-10-03 至 2026-10-07 的 Phase E 计数保持不变，下一交易日按本地日历检查。

估值补丁退役的历史设计和当前公开仓库状态见 `docs/patch-retirement-watch.md`；在对应哨兵代码
恢复并通过验收前，不得把“每日自动观察”当成已启用能力。

### 3.1 后台栈端口与自检

`ops/start_obs_stack.ps1` 一次拉起 9 项常驻服务（幂等：已在跑的只报 `OK`，重复执行不起第二个），
结束前对 7 个端口做 TCP 自检：

| 端口 | 服务 | 就绪判据 |
|---|---|---|
| 8000 | Web 看板 `src/dashboard.py` | `GET /` 返回 200 |
| 3000 | Grafana | 端口监听 |
| 9090 | Prometheus | `/api/v1/targets` 中 `job=astock-db` 为 `up` |
| 9093 | Alertmanager | `GET /-/healthy` 返回 `OK` |
| 9111 | `ops/alert_hook.py` 告警接收端 | `GET /` 返回 200 |
| 9101 | `src/metrics_server.py` | `/metrics` 可读 |
| 6379 | Redis（Celery broker/backend） | `redis-cli ping` 返回 `PONG` |

链路：`metrics_server(9101) → Prometheus(9090) → Alertmanager(9093) → alert_hook(9111) → logs/alerts.log`
（设 `DING_WEBHOOK_URL` 时同步转发钉钉）。看板的“全量数据库更新”走 `Celery → Redis(6379)`。

**2026-09-14 修复的三个静默故障**（此前 Redis / Prometheus / 告警链一直没真正起来，
脚本却报"完成"，因为旧版只打印 `START` 而不校验端口）：

| 故障 | 根因 | 修法 |
|---|---|---|
| Redis 从未启动（6379 一直 closed） | `Ensure-Exe` 传了空参数 `@()`，`Start-Process -ArgumentList` 参数校验直接抛错 | 空参数时不传 `-ArgumentList`；显式传 `redis.windows.conf`，工作目录设为 `obs-stack\redis` |
| Prometheus 从未启动（9090 一直 closed） | `--config.file=prometheus.yml` 是相对路径，但 `-WorkingDirectory` 是项目根，该文件不在那里 | 配置改绝对路径 `ops\prometheus.yml`（其 `rule_files` 相对配置目录解析到 `ops\alert_rules.yml`），tsdb 指到 `obs-stack\prom\data` |
| 告警链全断（9093/9111 无进程） | 脚本本来就不启动 Alertmanager 与 alert_hook | 新增第 3、4 步；并把端口自检从"打印"改成真正的 TCP 探测 |

## 4. 回测与检测

```bash
python src/rerun_vnpy_all.py                 # 滚动窗口回测样本
python src/backtest_with_gate.py             # 门控连续重放(当前配置)
python src/overfitting_test.py --html        # 过拟合与稳健性检测
python scripts/nonoverlap_rerun.py       # 非重叠窗口样本(盘后/空闲时)
python scripts/pbo_sweep.py              # PBO 参数扫描 + CSCV(约 50 分钟, 可断点续跑)
```

## 5. 运维注意事项

- 守护进程与引擎使用外部 Python(见 `TRAE_PYTHON`);确认该解释器含全部依赖。
- 交易日引擎在 08:30 由 `daemon.py` 拉起,收盘选股在 **19:10** 执行(`daemon.py` 的
  `MARKET_CLOSE`);崩溃自动拉起由守护负责。
  **[2026-09-28 更正]** 此前本行写的是「收盘 15:05 执行选股」, 那是**过期**的 ——
  15:05 自 2026-09-08 起只是**维护窗口**`--maint`(非交易日)的触发点与收盘窗口的**起点**,
  真正的收盘选股已后移至 19:10(数据商收盘后 1-4 小时才出全量日线/估值)。
  该过期描述曾直接导致一次误判: 把非交易日 15:06 的 `--maint` 产物当成了收盘选股产物。
- 数据文件(`data/h5i/market.db`)被进程独占时,回测/重建脚本会因文件锁失败——请在收盘后或停止守护时执行批量任务。
- 代码更新后重启顺序:`gate_refresh_daemon.py`(IC 门控)→ `daemon.py`(主调度)→ 视需要 `dashboard.py`。

### 5.1 代码部署清单（强制）

常驻 Python 进程会缓存已经导入的模块。**磁盘文件更新不代表运行时已经生效**；
2026-10-01 实测自然发布到 16:31:44 仍在使用旧版 `health_state`，只有重启守护后
16:33:52 的快照才与磁盘代码一致。因此本仓选择“重启并验证”，不采用运行中热重载
（热重载会留下旧对象、旧线程和跨模块引用，无法保证进程状态整体一致）。

每次修改 `src/`、运行入口或健康判据后，必须依次完成：

1. 运行相关测试，确认退出码为 0；
2. 仅重启守护（通常无需重启独立行情引擎）：
   `pwsh -File ops/service_control.ps1 -Action restart -SkipStockdb`；
3. 等待启动时健康快照发布，确认 `data/health/state.json` 的 `ts` 晚于重启时间；
4. 确认 `observed.code_version.scope=src/**/*.py`、`matches=true`，并且
   `loaded_sha256 == disk_sha256`；
5. 运行 `python scripts/verify_runtime_code_version.py`，必须返回 0；
6. 若本次涉及门禁，再确认快照 `gate_verdict` 与最近一轮
   `daily_summary.json/steps.datasource_gate` 完全一致；
7. 最后才能提交、推送或宣布部署完成。

任一步失败都视为部署未完成。尤其不得把“周期快照已刷新”误当成“新代码已加载”。
