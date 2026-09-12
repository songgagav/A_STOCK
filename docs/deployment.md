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
python src/daemon.py                       # 交易日守护: 引擎 08:30 / 收盘 15:05 / 崩溃自动拉起
python src/realtime_engine.py --once       # 盘中撮合单次
python src/dashboard.py --port 8000        # Web 面板
```

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
