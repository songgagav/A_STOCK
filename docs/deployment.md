# 部署指南

## 1. 环境准备

- Python 3.14(核心依赖见 `requirements_314.txt`)
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
| `STOCKDB_ROOT` | 本地行情镜像根(parquet 分片/单文件全量) |
| `ARCTIC_URI` | ArcticDB LMDB,如 `lmdb://<path>/arcticdb` |
| `PYBAO_DIR` | 本地行情引擎 SDK(可选) |
| `TRAE_PYTHON` | 守护进程使用的外部 Python(含依赖) |
| `FACTOR_HEALTH_ENABLED` | 因子健康隔离开关(默认 `1`;`0` 关闭) |
| `OVERFIT_RESULTS_FILE` | 过拟合检测窗口样本源(可选覆盖) |

> Windows 持久化示例:`setx STOCKDB_ROOT "E:\data\stockdb"`。**修改环境变量后需新开终端再启动守护**,否则新进程读不到。

## 3. 运行模式

```bash
# 一次性/日频
python src/run_daily.py                    # 收盘选股主流程
python src/gate_refresh_daemon.py          # IC 缓存刷新守护 (交易日 16:05-16:50 窗口)
python src/sentinel_daemon.py              # 估值覆盖率哨兵守护 (每日 18:30; 报缺口才补 pe_ttm)
python src/daemon.py                       # 交易日守护: 引擎 08:30 / 收盘 15:05 / 崩溃自动拉起
python src/realtime_engine.py --once       # 盘中撮合单次
python src/dashboard.py --port 8000        # Web 面板

# 后台常驻(观测栈 + 上述守护) 一键拉起, 幂等, 重复执行不会起第二个
powershell -ExecutionPolicy Bypass -File ops/start_obs_stack.ps1
```

`src/sentinel_daemon.py` 由 `ops/start_obs_stack.ps1` 拉起，日志
`logs/sentinel_daemon.log`，状态 `data/sentinel_last.json`；手动补跑用 `--once`。
背景见 `docs/patch-retirement-watch.md`（补丁退役观察期）。

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
- 交易日引擎在 08:30 由 `daemon.py` 拉起,收盘 15:05 执行选股;崩溃自动拉起由守护负责。
- 数据文件(`data/h5i/market.db`)被进程独占时,回测/重建脚本会因文件锁失败——请在收盘后或停止守护时执行批量任务。
- 代码更新后重启顺序:`gate_refresh_daemon.py`(IC 门控)→ `daemon.py`(主调度)→ 视需要 `dashboard.py`。
